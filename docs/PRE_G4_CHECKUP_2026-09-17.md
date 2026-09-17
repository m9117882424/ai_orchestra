# Чекап G0–G3 перед G4 — 17.09.2026

## Решение

**CI закрытия G3 подтверждён. Переход к реализации G4 рекомендовано задержать до
приёмки исправлений этого чекапа.** Production работоспособен; признаков текущего
зависания execution/runner jobs в проверенном срезе нет. Новые негативные проверки
нашли дефекты, которых не было в исходной зелёной suite.

Исправления подготовлены в ветке `audit/pre-g4` от
`775019c5d0814c5a63e4ff0f4922ce6c196f2eac`. Они не опубликованы и не развёрнуты.
Это выборочный инженерный аудит критических границ, а не доказательство отсутствия
всех дефектов или полноценный penetration/chaos test.

## Подтверждённая исходная точка

| Объект | Проверенный результат |
| --- | --- |
| Main / документация закрытия | `775019c5d0814c5a63e4ff0f4922ce6c196f2eac`, PR #29 |
| CI | [#400](https://github.com/m9117882424/ai_orchestra/actions/runs/35188890168), run `35188890168`, `completed/success`, завершение 06:17:49 UTC |
| CI job | `105096839975`; все содержательные steps success, пропущенных gates нет |
| Production Git | `8cea45110438636e266edc97ee87fd1babb0aa08`, чистый рабочий каталог |
| Разница production/main | Документация приёмки; runtime соответствует принятому G3 |
| Schema | CLI проверил revision **и physical shape**: `20260916_0009` |
| Runtime | 9 running/healthy сервисов; RestartCount каждого 0 в текущем срезе |
| Broker | `ai-orchestra-runnerd` active; socket `root:ai-orchestra-runner`, mode 0660 |
| Control Plane | локальный `/health` вернул `{"status":"ok"}` |
| Execution queue | 2 completed, 2 cancelled, 1 failed; активных нет |
| Runner queue | 3 completed; активных нет; оставшихся disposable runner containers нет |
| Диск | 291 GiB всего, 215 GiB доступно, занято 27% |

Проверка CI включала статусы Docker build, disposable isolation, durable manager
recovery, Temporal PoC, DR, PostgreSQL migration semantics, toolchain и ownership.
Эти Docker/DR результаты относятся к **исходному commit #400**, не к новой ветке.

## Реестр дефектов и исправлений

Приоритеты: P1 — исправить до следующего инженерного этапа; P2 — устранить в
текущем hardening-пакете. Наличие дефекта не означает, что он уже проявился в production.

| ID / приоритет | Подтверждение и эффект | Подготовленное исправление |
| --- | --- | --- |
| F1 / P1 | `os.walk()` в обеих реализациях snapshot без `onerror` молча пропускал недоступный подкаталог. Получался digest неполного дерева вместо отказа. Два fault-injection теста воспроизвели дефект. | Ошибка обхода превращается в `SourceSnapshotError`; одинаковое поведение execution-worker и sandbox. |
| F2 / P1 | Общий migration runbook останавливал base services, но не optional Runner Manager. `make migrate` проверял лишь наличие PostgreSQL, позволяя миграцию при работающих writers. | Guard до backup/schema action проверяет все шесть lifecycle writers, включая Runner Manager; runbook содержит overlay build/stop/start и ожидание завершения jobs. |
| F3 / P2 | Terminal parser принимал `completed` с exit 1/False, `failed` с exit 0, неhex image ID, version 2, строковый output_truncated и объект stdout. Семь негативных тестов воспроизвели принятие. Completion gate дополнительно проверял exit 0, но неверное durable evidence всё равно могло сохраняться. | Строгая проверка protocol version/types/image ID/status↔exit; противоречивый ответ становится `cleanup_uncertain`. |
| F4 / P2 | Checkpoint допускал 128 argv элементов, runnerd — 64: принятый checkpoint мог быть затем отвергнут брокером. | Единый предел 64, контрактные тесты границ 64/65 между двумя парсерами. |
| F5 / P2 | README утверждал, что G2.3 ещё не принят; backup был описан как format 2; migration runbook называл head 0007; roadmap сохранял устаревшие ограничения G0. | Актуализированы статус, format 3, schema 0009 и эксплуатационные инструкции; добавлено точное подтверждение #400. |

F1 исправляет полноту обхода, но не превращает изменяемую файловую систему в
атомарный snapshot. Дальнейшая защита от активных конкурентных filesystem races
требует отдельной оценки; в этом чекапе такой гарантии не заявляем.

## Проверка production evidence

Из БД повторно прочитан execution `67080907-2a58-4678-8a75-559aa15bd940`:
`completed / manager_review`, base `8cea45110438636e266edc97ee87fd1babb0aa08`.

Runner jobs:

- `a0c4802f-a59b-4062-9591-e22448c1fa4f` — completed, exit 0, cleanup true;
- `16c4e27d-f687-4ed4-a3fd-a2df95c4dcc0` — completed, exit 0, cleanup true.

Оба с snapshot
`d58c40def50f4823039aa943d93b2c69566c1d1a2eb04465c76543850d21a5a1`
и image
`sha256:235bc849835769854af3d08557f616e0294b8207b9ef7846147595b65d4a7c8e`.

Повторно вычисленные SHA-256 файлов backup совпали с актом приёмки:

- `ai-orchestra-20260917T055217Z.tar.gz`:
  `406b4c2fda8ea4323a16324b20064014ca686f68b07403b798a688f1210df7a2`;
- `ai-orchestra-20260917T060514Z.tar.gz`:
  `e20552739d015b1ba2d39601aae0134b55fba24c7a6762505b2e9f5e64c34b2a`.

Это проверка идентичности архивов, **не новый restore drill и не подтверждение
off-host копии**. Новый destructive smoke на production-хосте не запускался.

## Оценка пройденных gates

| Контур | Основание оценки | Ограничение |
| --- | --- | --- |
| G0 / capability boundaries | Прочитаны актуальные policy, OpenCode permissions и CI security gates; production containers не privileged | Полный аудит сети/внешнего периметра и penetration test не выполнялся |
| G1 / durable core | Повторный полный unit/API прогон, CI recovery/DR/PostgreSQL gates, production schema shape и очереди | Production restart/failure injection в этом чекапе не выполнялся |
| G2 / repositories/workspaces | Полная suite, review preflight/identity и trust recheck, CI Git worker/ownership gates | Не проверялся заново каждый Git provider и credential profile |
| G3 / runner | Review broker, entrypoint, manager, checkpoint, snapshot и completion gate; новые негативные тесты; сверка production jobs | Изоляция same-host Docker; не отдельная VM; найдены F1–F4 |
| Change control | Финальный #400 green; runtime/main divergence объяснена docs-only PR | Новые исправления требуют собственного CI, review и отдельного rollout |

Скрипты destructive smoke теперь используют отдельные Compose namespaces,
image tags и временные backup roots. Прежний механизм попадания в фиксированный
production project устранён в reviewed baseline. Это статическая проверка кода
и существующих CI результатов; она не даёт оснований запускать любые smoke
в `/opt/ai_orchestra` без проверки фактически разрешённой Compose-конфигурации.

## Проверки подготовленных изменений

- Исходная suite до изменений: **221 passed**.
- Новые fault-injection/terminal regressions до fixes: **9 failed, 12 passed** —
  подтверждение, что тесты обнаруживают дефекты baseline.
- После fixes: `python -m pytest control_plane/tests runner/tests -q` —
  **238 passed**, включая 17 новых parametrized cases.
- `python scripts/verify_dependency_locks.py` — green, runtime 28 / dev 36 packages.
- `python -m compileall -q control_plane/app control_plane/tests runner` — green.
- `bash -n scripts/migrate-control-plane.sh` — green.
- `cmp control_plane/app/source_snapshot.py runner/source_snapshot.py` — identical.
- `git diff --check` — clean.

Тесты выполнены Python 3.12 из существующего окружения
`/workspace/scratch/90337f3559d5/.venv-g2/bin/python`; новая установка строго из
dev lock в этом чекапе не выполнялась. Есть одно стороннее DeprecationWarning
Starlette/AnyIO, без падений.

Локально Docker CLI/daemon и ShellCheck недоступны: новый `make validate`,
container build, real Docker smokes и ShellCheck **не заявляются выполненными**.
Их необходимо подтвердить новым CI после публикации исправлений.

## Что нельзя незаметно перенести в G4

1. Приёмочный G3 E2E создавал один текстовый файл и проверял diff/content. Это
   подтверждает маршрут, identity и cleanup, но не произвольный application build.
   Нужен отдельный E2E на небольшом реальном проекте с изменением кода,
   отрицательным тестом, исправлением и независимыми QA/reviewer verdicts.
2. Базовый runner Dockerfile не устанавливает project dependencies, pytest или
   Node; network=none. Для реальных стеков нужны заранее подготовленные,
   утверждённые offline toolchain/dependency profiles. Не включать общий egress
   как незаметный обход ограничения.
3. Completion gate проверяет наличие успешного checkpoint для snapshot, но не
   семантическую достаточность команд и QA. Даже `true` является успешной командой.
   В G4 нужны явные required checks и привязанные к snapshot verdicts; prompt
   не заменяет проверку полноты result package.
4. Child-run lineage/role/model/cost и доказательство независимости QA не следует
   восстанавливать только из текста Lead. Это открытый G4 scope, а не уже
   реализованная durable гарантия.
5. Runner Manager heartbeat проверяет жизнь процесса, а не достижимость broker.
   Bind-mount отдельного socket требует recreate manager после restart runnerd.
   Runbook это фиксирует; полноценный broker readiness и reconnect drill —
   отдельный эксплуатационный сценарий для G4.

## Условия снятия hold

1. Review и публикация hardening-ветки отдельным PR, новый полный green CI.
2. Отдельно согласованный rollout с backup и обновлением нужных образов;
   snapshot fix затрагивает execution-worker **и** disposable image.
3. Post-rollout schema/runtime/broker checks и небольшой runner E2E с exact
   source/image/cleanup evidence.
4. После этого старт G4 с явно согласованным realistic-project acceptance.

`policy/AGENTS.md` требует явного подтверждения для push/PR и production rollout.
В этом чекапе внешних Git mutations и production runtime изменений не было.
