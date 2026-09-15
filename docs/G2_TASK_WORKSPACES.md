# G2.3 Durable Task Workspaces

## Статус и назначение

G2.3 — engineering candidate для правила **one task execution = one immutable
repository binding = one isolated Git workspace**. До этого инкремента Repo
Manager уже умел безопасно получить read-only mirror, но execution не был
технически привязан к конкретному commit и отдельному каталогу.

Инкремент не даёт AI права `push`, создавать PR, выполнять merge или deploy. Он
также не является disposable execution sandbox: исполнение недоверенных build и
dependency scripts остаётся задачей G3.

## Trust boundaries

| Компонент | Mirror | Task workspace | Git credential | Model API |
|---|---:|---:|---:|---:|
| Repo Manager | read/write | нет | read-only profile | нет |
| Workspace Manager | read-only | read/write | нет | нет |
| Execution Worker | нет | read-only | нет | нет |
| OpenCode / AI | нет | read/write | нет | только Model Gateway |
| Control Plane | нет | нет | нет | нет |

`workspace-manager` находится только в `control-db`, не имеет egress, порта,
provider key или Git credential. Он создаёт workspace исключительно из уже
проверенного локального mirror. `execution-worker` не может изменить workspace,
а OpenCode не видит mirror и не может получить remote credential. `model-net`
является internal Docker network: прямой Internet egress OpenCode закрыт, а
provider network доступен только Model Router.

Named volume принадлежит UID/GID `10001:10001` и имеет режим `0700`.
One-shot initializer без сети и с read-only rootfs проверяет этот invariant до
старта writers. OpenCode сохраняет `cap_drop: ALL` и получает обратно только
`DAC_OVERRIDE` и `FOWNER`, необходимые его UID 0 для записи и изменения mode в
этом volume; Docker socket, mirror, Git credentials и control-plane/provider
secrets ему по-прежнему недоступны. Execution Worker монтирует тот же volume
только read-only. Workspace Manager получает те же две filesystem capabilities
только внутри своих mounts, чтобы гарантированно инспектировать, архивировать и
удалять root-owned результаты OpenCode; rootfs остаётся read-only, mirror mount —
read-only, а сеть — только `control-db`.

## Durable lifecycle

1. Manager назначает задаче `ready` repository.
2. `POST /api/tasks/{task_id}/execute` одной транзакцией фиксирует:
   - execution contract v2;
   - точные repository ID и base commit;
   - новый workspace ID, branch и абсолютный path;
   - execution в `preparing`, workspace в `pending`.
3. Workspace Manager забирает durable lease и проверяет binding повторно.
   Непосредственно перед filesystem effect он также повторно проверяет, что
   Registry всё ещё разрешает `ready` repository. Отзыв trust останавливает
   подготовку, а временная повторная validation переводит её в bounded retry.
4. Из mirror создаётся standalone Git repository без remote. Hooks, fsmonitor,
   submodules и сетевые protocols отключены.
5. После проверок каталог публикуется атомарным rename. Только затем одна
   транзакция, удерживающая повторную блокировку Registry row, переводит workspace
   в `ready`, а execution — в `queued`. Поэтому отзыв repository trust во время
   checkout не может пересечь границу публикации очереди.
6. Execution Worker сверяет DB binding и read-only filesystem evidence, затем
    повторяет проверку непосредственно перед единственным prompt, запускающим
    inference. На границе prompt он удерживает row lock Repository и требует
    `enabled + ready`: конкурентный отзыв trust либо завершается первым и
    блокирует inference, либо ждёт завершения уже авторизованного prompt POST.
7. Все OpenCode calls выполняются с `X-OpenCode-Directory`, привязанным к этому
   workspace.
8. После `completed`, `failed`, timeout или cancellation workspace ставится в
   очередь инспекции. Результат хранит HEAD, tree, change digest и число
   изменённых/untracked/ignored entries.
9. Изменённый или неоднозначный workspace сохраняется. Автоматическое удаление
   возможно только после доказанной clean-инспекции, терминального execution и
   versioned запроса Manager.

Ранняя отмена до завершения preflight сразу делает execution терминальным и
ставит cleanup в очередь. Отсутствующий каталог в таком состоянии является
нормальным и обрабатывается идемпотентно.

## Immutable binding и evidence

Execution contract v2 хранит:

- `repository_id`, `workspace_id`, `base_commit`, `workspace_path`;
- исходный Git tree;
- digest workspace manifest и время durable preflight;
- digest повторной runtime-проверки и её время.

Manifest находится в `.git/ai-orchestra-workspace.json`, имеет точную форму,
режим `0400` и SHA-256 по каноническому JSON. Его identity должна совпасть с
двумя независимыми DB rows: `execution_runs` и `task_workspaces`. Самоподписанный
поддельный manifest недостаточен: worker сравнивает его digest и поля с durable
evidence в БД.

Перед inference дополнительно проверяются:

- canonical workspace path и отсутствие symlink на корне;
- manifest type, link count, permissions, shape и digest;
- exact HEAD, tree и task branch;
- чистый Git status, включая untracked и ignored files;
- количество tracked entries;
- каждый tracked entry: regular file либо symlink, остающийся внутри workspace
  и не ведущий в `.git`.

Любое расхождение переводит execution в `workspace_binding_rejected`, workspace
в `invalid`, задачу в `failed` и создаёт audit event. Inference при этом не
запускается.

## Repository preflight

До checkout Workspace Manager проверяет:

- canonical UUID, commit и branch names;
- наличие exact commit в конкретном mirror;
- допустимый Git tree;
- запрет gitlinks/submodules;
- лимит количества файлов, размера одного blob и суммарного workspace;
- резерв свободного диска;
- безопасные tracked paths и symlinks.

Staging и cleanup используют per-workspace OS lock. Чтение mirror защищено
shared lock, совместимым с exclusive lock Repo Manager. Crash до DB commit
восстанавливается новым lease generation: staging удаляется, готовый каталог
проверяется и переиспользуется, а устаревшее поколение не может записать result.

## Workspace API

```text
GET   /api/workspaces
GET   /api/workspaces/{workspace_id}
POST  /api/workspaces/{workspace_id}/cleanup
PATCH /api/tasks/{task_id}/repository
```

Cleanup mutation требует `expected_version`. Запрос со stale version либо для
изменённого, непроверенного или активного workspace получает `409 Conflict`.

## Backup / restore contract v2

`scripts/backup.sh` координированно приостанавливает Control Plane,
Execution Worker, Workspace Manager и OpenCode, создаёт bounded PostgreSQL dump
и `task-workspaces.tar.gz`, затем возобновляет writers до упаковки и проверки.
Trap возобновляет writers и при ошибке.

Verifier работает с приватным snapshot входного архива, проверяет canonical
paths/types/limits, полный безопасный inventory `SHA256SUMS` и вложенный workspace
archive. Hardlinks, devices, escaping symlinks и неожиданные workspace roots
запрещены.

Restore drill восстанавливает БД только в disposable PostgreSQL, извлекает
workspace snapshot в изолированный каталог и сверяет DB state с наличием
каталогов и manifest binding. Формат v1 остаётся читаемым только если после
миграции в БД нет workspace rows.

## Operational states

| Status | Значение |
|---|---|
| `pending` / `preparing` | создание ещё не завершено |
| `unavailable` | transient failure, ожидается bounded retry |
| `ready` | durable preflight завершён, execution может быть queued/running |
| `inspection_pending` / `inspecting` | терминальный результат сверяется с Git |
| `retained` | каталог сохранён; cleanup требует отдельного безопасного решения |
| `cleanup_pending` / `cleaning` | идемпотентное удаление запрошено/выполняется |
| `removed` | отсутствие каталога подтверждено |
| `invalid` | binding/evidence неоднозначны; автоматическое выполнение закрыто |

Leases имеют owner, expiry, generation и record version. Retry использует
ограниченный exponential backoff. Никакой timeout, exception или неизвестное
состояние не превращается в success.

## Acceptance evidence перед rollout

Обязательные gates:

1. fresh schema и migration `0006 -> 0007`, включая сохранение contract v1;
2. реальный локальный Git mirror → workspace → runtime preflight → prompt →
   terminal inspection lifecycle;
3. tamper, manifest forgery, symlink escape, gitlink, ignored artifact, stale
   lease и DB-binding race tests;
4. backup v2 positive/hostile verifier tests и Docker backup/restore smoke;
5. Compose/static isolation checks и сборка каждого target;
6. PR review и зелёный CI;
7. migration-first production rollout с verified backup, нулём active executions,
   schema check, health и smoke.

## Явные ограничения

- Workspace — логическая изоляция между задачами, но ещё не G3 sandbox для
  запуска недоверенного кода.
- OpenCode policy запрещает `external_directory`; это ограничивает штатные tools
  назначенным task workspace, но не заменяет будущую OS-level изоляцию G3.
- OpenCode не имеет прямого egress и по назначению может писать только в task
  workspace volume; это не разрешает внешние Git actions.
- Mirror после DR пересоздаётся Repo Manager из remote; credentials восстанавливаются
  отдельным secret process.
- Push/PR/merge/deploy остаются DENY до content-addressed approvals и
  reconciliation G5.
- Production Temporal по-прежнему не включён; G1 использует текущий durable DB
  worker, а Temporal restart proof остаётся изолированным PoC.
