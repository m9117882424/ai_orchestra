# G2.2 Trusted Repo Manager

## Назначение

Trusted Repo Manager — единственный компонент AI Orchestra, которому разрешены
read-only Git network operations. Он берёт задания из Repository Registry,
проверяет remote и поддерживает bare mirror. Control Plane только ставит проверку
в очередь; OpenCode и execution-worker не получают Git credentials и не могут
вызвать Repo Manager по сети.

Сам G2.2 не создаёт task workspace и не даёт права `push`, PR, merge или deploy.
Следующий G2.3 использует его `ready` mirror через отдельный read-only mount;
полный контракт описан в
[`G2_TASK_WORKSPACES.md`](G2_TASK_WORKSPACES.md). Репозиторий со статусом,
отличным от `ready`, не считается пригодным для execution preflight.

## Границы доверия

| Компонент | Git credential | Git egress | Mirror volume | Control DB |
|---|---:|---:|---:|---:|
| Repo Manager | Да, read-only profile | Да, отдельная сеть | Read/write | Да |
| Control Plane | Нет | Нет | Нет | Да |
| execution-worker | Нет | Нет | Нет | Да |
| OpenCode / AI | Нет | Нет | Нет | Нет |
| Model Router | Нет | Provider egress | Нет | Нет |

`repo-manager` не публикует порт, не монтирует Docker socket, работает не от
root, с read-only root filesystem и отдельным named volume
`repository-mirrors`; CPU/RAM/PID и временный filesystem ограничены. Этот volume
является восстановимым cache: source of truth —
remote плюс Registry. Он намеренно не входит в backup и после DR заполняется
повторной проверкой.

Repo Manager собирается отдельным Docker target с Git/CA toolchain. Образы
Control Plane и Execution Worker этот Git executable не содержат.

## Credential boundary

Секреты находятся только в ignored-файле `.env.repositories` с правами `0600`.
Registry хранит только `auth_profile_ref`. Формат файла:

```dotenv
REPO_MANAGER_AUTH_PROFILES_JSON='{"git-readonly-primary":{"host":"github.com","username":"x-access-token","password":"REPLACE_WITH_READ_ONLY_TOKEN"}}'
```

Для public repositories используется пустой объект:

```dotenv
REPO_MANAGER_AUTH_PROFILES_JSON='{}'
```

Каждый profile жёстко привязан к каноническому host. Repo Manager передаёт
выбранную пару username/password только дочернему Git process через isolated
environment и host-checking askpass helper. Credential получают только сетевые
`ls-remote`/`fetch`; локальные `init`, `rev-parse` и `fsck` запускаются без него.
Credential не попадает в URL, argv, Git config, БД, API, audit или application
logs. Изменение файла требует
перезапуска только Repo Manager:

```bash
chmod 600 .env.repositories
docker compose up -d --force-recreate repo-manager
```

Используйте provider token с минимальным read-only scope. Write-capable token не
соответствует текущей модели безопасности, даже если технически принимается
провайдером.

## Fail-closed network validation

Перед каждым `ls-remote`/`fetch` worker повторно нормализует Registry identity и:

1. разрешает только `https://...:443` без embedded credential, query или fragment;
2. получает все DNS addresses и отклоняет host целиком, если хотя бы один адрес
   не является global unicast (loopback/private/link-local/reserved запрещены);
3. закрепляет проверенный набор адресов для libcurl через
   non-expiring `http.curloptResolve` на всё время конкретного Git process,
   сохраняя TLS SNI/certificate check по исходному host;
4. удаляет proxy environment и обнуляет Git proxy/extra-header config;
5. устанавливает `http.followRedirects=false`: любой redirect является ошибкой;
6. разрешает transport только `https`, отключает interactive prompt, hooks и
   recursive submodules;
7. ограничивает timeout/low-speed, параллелизм HTTP и максимальный размер одного
   создаваемого Git-файла;
8. до запуска Git сохраняет на volume настроенный резерв свободного места
   (`REPO_MANAGER_MIN_FREE_BYTES`, по умолчанию 512 MiB) и уменьшает file limit
   до реально доступной ёмкости.

По умолчанию выполняется один fetch одновременно
(`REPO_MANAGER_MAX_ACTIVE=1`), чтобы два больших remote не конкурировали за один
и тот же disk reserve. Повышать параллелизм можно только после отдельного
capacity test и мониторинга volume.

Запрет redirect намеренно строже проверки redirect chain: неизвестный второй host
не получает ни запрос, ни credential. Remote, которому необходим redirect, надо
зарегистрировать сразу по его каноническому конечному HTTPS URL.

## Mirror lifecycle

- `git ls-remote --symref ... HEAD` определяет default branch и remote HEAD SHA;
- новый bare mirror собирается во временном каталоге и публикуется атомарным
  rename только после полного fetch и проверки;
- перед повторной попыткой worker удаляет только staging-каталоги этого
  repository ID и stale Git lock-файлы из его mirror, оставшиеся после аварийно
  завершившегося поколения;
- отдельный OS-level lock на repository ID не допускает одновременную запись в
  один mirror даже при ошибочном втором worker или восстановлении lease;
- существующий mirror обязан быть bare repository с ровно тем `origin`, который
  записан в Registry;
- fetch использует принудительный branch refspec, `--prune`, `--no-tags` и object
  fsck; рабочее дерево и untrusted scripts не создаются;
- fetched default-branch commit должен точно совпасть с объявленным remote HEAD;
  гонка изменения remote становится transient failure и повторяется позже;
- crash между filesystem effect и DB commit безопасен: следующий lease повторяет
  idempotent fetch и только затем фиксирует `ready`.

Каждый Git process создаётся в отдельной process group. При deadline Repo Manager
принудительно завершает всю группу и дожидается её reap, поэтому дочерний
`git-remote-https` не продолжает сеть или запись после зарегистрированного
timeout.

## Durable state и fencing

Alembic revision `20260909_0006` добавляет к Registry:

- `sync_generation` и пару lease owner/expiry;
- requested/started/finished/next timestamps;
- consecutive failure counter и безопасный error code;
- queue и lease indexes;
- checks для целостной lease-пары и доказательств статуса `ready`.

Claim выполняется через PostgreSQL row lock с `SKIP LOCKED`. Каждый claim
увеличивает generation и версию записи. Heartbeat, success и failure принимаются
только от текущего owner при совпадении generation, версии и непросроченного
lease. Изменение policy владельцем или takeover после expiry делает старый
результат недействительным.

Transient failure переводит запись в `unavailable` и использует ограниченный
exponential backoff. Security/policy violation переводит её в `invalid` без
автоматического retry. Ручная повторная проверка выполняется versioned mutation:

```text
POST /api/repositories/{repository_id}/validate
{"expected_version": 4}
```

Отключение репозитория немедленно отзывает `ready` и убирает его из очереди.
Повторное включение или смена auth profile ставит новую проверку в очередь.

## Безопасные error codes

API и audit получают только классифицированный код, например:

- transient: `dns_resolution_failed`, `git_remote_unavailable`,
  `git_fetch_failed`, `git_operation_timed_out`, `auth_profile_unavailable`,
  `mirror_busy`, `storage_capacity_low`;
- terminal: `remote_address_forbidden`, `registry_identity_mismatch`,
  `auth_profile_host_mismatch`, `remote_default_branch_invalid`, `mirror_origin_mismatch`,
  `repository_size_limit_exceeded`.

Raw Git stderr намеренно отбрасывается, поскольку provider способен вернуть
credential-bearing diagnostics.

## Rollout с production revision 0005

Это migration-first изменение. Перед migration должны быть нулевые active
executions и проверенный backup. После `git pull --ff-only` сначала создайте новый
secret scope, иначе Compose обязан отказать:

```bash
make init
make preflight
make build
docker compose stop control-plane execution-worker repo-manager
make migrate
make schema-check
make up
docker compose ps
make smoke
```

Migration сохраняет identity/policy существующих Registry rows, но переводит их
в `pending_validation` и ставит enabled rows в очередь. Это исключает доверие к
старому operational state, созданному до появления trusted worker.

## Критерии приёмки G2.2

1. Git credential видит только Repo Manager service и выбранный Git child.
2. Private/mixed DNS answer, redirect, иной protocol и identity drift закрываются
   до статуса `ready`.
3. Новый mirror становится видимым только после fetch, HEAD reconciliation и fsck.
4. Stale/expired generation не может зафиксировать success или failure.
5. Worker crash восстанавливается новым generation без ложного `ready`.
6. В БД, API, audit и logs нет raw credential или raw Git stderr.
7. Runtime schema guard проверяет revision `0006`, checks и indexes.
8. Legacy `0001` и production `0005` проходят migration/restore tests с
   сохранением бизнес-данных.
9. Исчерпанный storage reserve останавливает Git до fetch и уходит в
   контролируемый retry без заполнения системного диска.

## Следующий gate

G2.3 добавляет task workspace lifecycle, обязательный preflight, immutable
execution binding и conservative recovery/cleanup. Отдельная disposable
execution boundary для untrusted code остаётся G3.

Текущая реализация также использует общий PostgreSQL role Control Plane для
операционного состояния Repo Manager. Выделение минимально привилегированного DB
role остаётся отдельным hardening-инкрементом; Git credentials при этом уже
изолированы в отдельном process/env boundary.

Наличие `ready` mirror само по себе не разрешает inference: G2.3 дополнительно
требует успешный workspace preflight. Push/PR/merge остаются закрыты до G5.

## Implementation references

- [Git 2.39 HTTP configuration](https://git-scm.com/docs/git-config/2.39.0)
- [libcurl CURLOPT_RESOLVE](https://curl.se/libcurl/c/CURLOPT_RESOLVE.html)
- [Git protocol environment](https://git-scm.com/docs/git)
