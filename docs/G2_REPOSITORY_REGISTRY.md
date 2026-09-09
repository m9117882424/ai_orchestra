# G2.1 Repository Registry

## Назначение

Repository Registry — первый инкремент G2. Он хранит идентичность и политику
разрешённых Git-репозиториев, но сам не выполняет `clone`, `fetch`, `push` или
другие внешние действия.

Новая запись всегда создаётся как `pending_validation`. Реализованный в G2.2
отдельный trusted Repo Manager проверяет remote и переводит его в операционное
состояние. До статуса `ready` репозиторий не должен участвовать в execution.

## Данные реестра

- стабильный UUID и уникальное каноническое имя;
- нормализованный HTTPS remote, его identity и DNS host;
- определённый по host provider: GitHub, GitLab, Bitbucket или generic;
- непрозрачная ссылка на auth profile без credential;
- `default_branch`, последний известный commit и время fetch — read-only
  operational state, которым владеет Repo Manager;
- `enabled` и validation status;
- execution profile;
- assurance tier и обязательный domain profile для `regulated-critical`;
- монотонная версия записи для optimistic concurrency;
- timestamps и атомарный audit trail.

## Fail-closed правила remote

На границе API принимается только однозначный HTTPS URL:

- без username, password, token, query и fragment;
- без IP literal, localhost, зарезервированных и single-label host;
- без нестандартного порта;
- без percent-encoding, обратных слешей, `.`/`..` и неоднозначных path segments;
- с владельцем и именем репозитория для известных SaaS-провайдеров.

Варианты одного GitHub/GitLab/Bitbucket remote с разным регистром или суффиксом
`.git` получают одну `remote_identity` и не могут быть зарегистрированы дважды.

Эта проверка не заменяет сетевую политику. Перед каждым fetch Repo Manager
повторно проверяет URL и все DNS addresses, закрепляет разрешённые addresses для
TLS-соединения и запрещает redirect целиком. Registry не выполняет DNS-запросов и
не делает ложный вывод о доступности remote.

## Секреты

В таблице нет колонок для token, password, private key или credential. Поле
`auth_profile_ref` принимает только короткий identifier и отклоняет распространённые
token prefixes. Сам credential принадлежит Repo Manager secret scope и
никогда не передаётся Control Plane response, OpenCode или LLM.

## Состояние и конкурентные изменения

- Manager может регистрировать, читать, фильтровать, отключать и менять policy
  metadata.
- Manager не может устанавливать `ready`, provider, branch, commit или fetch time.
- Удаление через API отсутствует: отключение сохраняет историю и ссылки аудита.
- PATCH требует `expected_version`; stale update получает `409 Conflict`.
- PostgreSQL row lock и версия предотвращают lost update.
- Уникальные индексы закрывают race между параллельными регистрациями.
- Registry row и audit event фиксируются одной транзакцией.
- Database check constraints дублируют критичные enum/profile инварианты.

## API G2.1

```text
GET   /api/repositories
GET   /api/repositories/{repository_id}
POST  /api/repositories
PATCH /api/repositories/{repository_id}
POST  /api/repositories/{repository_id}/validate
```

Mutation endpoints требуют manager authentication и `X-Control-Request`, как и
остальной Control Plane.

## Миграция

Alembic revision `20260908_0005` создаёт только новую пустую таблицу
`repositories` и её constraints/indexes. Существующие task/execution данные не
изменяются. Новый runtime обязан запускаться только после migration-first rollout
по общему schema runbook.

## Критерии приёмки этого инкремента

1. Небезопасный или неоднозначный URL отклоняется до записи в БД.
2. Имя и remote identity уникальны даже при конкурентной регистрации.
3. Новая запись не может объявить себя проверенной.
4. Operational fields нельзя подменить через Manager API.
5. Stale update не перезаписывает более новую policy.
6. Auth profile сохраняется только как reference и не попадает в audit details.
7. Legacy `0001` и production `0004` безопасно мигрируют до `0005` с сохранением
   существующих данных.
8. Schema drift по обязательным checks/indexes блокирует production startup.

## Что остаётся в G2

- task branch/worktree lifecycle;
- обязательный workspace preflight до первого LLM inference;
- привязка execution к immutable repository/base/worktree identity;
- recovery и cleanup без потери незакоммиченных данных.

Trusted Repo Manager, credential boundary и read-only mirror lifecycle закрыты в
[`G2_TRUSTED_REPO_MANAGER.md`](G2_TRUSTED_REPO_MANAGER.md).

Push/PR/merge autonomy не входит в G2. Она остаётся закрытой до content-addressed
approval и external-action reconciliation в G5.
