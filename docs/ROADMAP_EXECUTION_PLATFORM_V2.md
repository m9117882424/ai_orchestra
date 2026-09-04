# AI Orchestra — Execution Platform V2 Roadmap

## Цель

Превратить AI Orchestra из Control Plane с одиночным OpenCode execution в полноценный виртуальный отдел разработки, способный безопасно работать с произвольными Git-репозиториями, выполнять задачи в изолированных workspace, делегировать работу независимым ролям, запускать тесты и отдавать руководителю проверяемый пакет результата.

## Базовые принципы

1. **Repository-agnostic** — ни один проект не зашивается в конфигурацию. Любой поддерживаемый Git URL регистрируется через Repository Registry.
2. **Secrets never reach agents** — Git credentials, provider admin credentials и production secrets не передаются OpenCode/агентам.
3. **One task = one worktree + one execution sandbox** — параллельные задачи не должны портить общий checkout.
4. **Fail closed** — push, PR creation, merge, deploy, secret access, external write и financial execution требуют policy/approval.
5. **Observable execution** — состояние определяется реальными событиями worker/OpenCode/tool calls, а не браузерным polling или искусственным процентом прогресса.
6. **Independent QA** — Coder не принимает собственную работу. Для важных изменений Reviewer/QA используют отдельную роль, при необходимости отдельную модель.
7. **Recoverable** — рестарт браузера, Control Plane или worker не должен терять задачу.
8. **Auditable** — все действия, approvals, git refs, tests, artifacts и расходы привязаны к task/execution.

---

## Milestone 1 — Repository Workspace Foundation

### Repository Registry

Храним для каждого репозитория:
- id / name;
- remote URL;
- provider type: github / gitlab / bitbucket / generic_git;
- auth profile reference без самого секрета;
- default branch;
- mirror path;
- enabled/disabled;
- last fetch status/time;
- last known commit;
- project execution profile.

### Repo Manager

Отдельный сервис с минимальным API:
- register/validate repository;
- clone mirror;
- fetch/prune;
- detect default branch;
- create task worktree;
- inspect status/diff;
- commit;
- cleanup worktree;
- push только после scoped approval.

Repo Manager владеет Git credentials. OpenCode их не получает.

### Worktree lifecycle

Для каждой задачи:

`repository mirror -> task branch -> /workspace/worktrees/<task-id>`

Именование веток по умолчанию:

`orchestra/<task-id>-<slug>`

Acceptance criteria:
- можно зарегистрировать новый публичный Git URL без правки compose;
- можно создать worktree от default branch;
- две задачи одного repo получают разные worktree;
- OpenCode видит только рабочие файлы задачи;
- main/default branch не модифицируется.

---

## Milestone 2 — Worker / Job Queue

Execution lifecycle больше не зависит от открытого браузера.

Состояния:

`queued -> provisioning -> planning -> architecture -> coding -> review -> tests -> qa -> waiting_approval -> ready -> completed/failed/cancelled`

Требования:
- durable queue на PostgreSQL либо Redis;
- heartbeat;
- max execution timeout;
- max tool timeout;
- retries с ограничением;
- stale execution watchdog;
- graceful restart/recovery;
- idempotent job handlers.

---

## Milestone 3 — Real Multi-Agent Orchestration

Orchestration Engine, а не prompt-only delegation.

Минимальный development workflow:

`Lead -> Architect -> Coder -> Reviewer -> QA -> Lead synthesis`

Для простых задач policy может сокращать workflow.

Для security/high-risk задач добавляется Risk/Security Reviewer.

Каждый child-run хранит:
- role;
- model alias;
- start/end;
- status;
- input/output tokens;
- tool calls;
- result;
- error;
- artifacts.

---

## Milestone 4 — Execution Sandbox / Toolchain

OpenCode base image содержит как минимум:
- git;
- ripgrep;
- fd/findutils;
- jq;
- curl;
- bash;
- Python;
- Node.js/npm/corepack;
- build-essential.

Проектные зависимости запускаются в disposable sandbox по execution profile.

Поддерживаемые профили на первом этапе:
- python;
- node;
- generic-shell.

Далее: Go, Java, .NET и специализированные профили.

Security:
- без Docker socket;
- без `/root` и host `.env`;
- ограниченный egress;
- CPU/RAM/PID/time quotas;
- отдельная файловая область задачи;
- недоверенные install/test scripts исполняются только в sandbox.

---

## Milestone 5 — Observability / Live Progress

В кабинете показываем только реальные события:
- current stage;
- current role;
- elapsed time;
- OpenCode/session state;
- model;
- tool calls;
- последние сообщения;
- test/build activity;
- heartbeat;
- tokens/cost.

Не показываем фиктивный `% готовности`.

Браузер только читает состояние. Worker определяет lifecycle.

---

## Milestone 6 — Result Package / Artifacts

Пакет приемки задачи:
- исходное задание;
- план;
- branch / base commit / head commit;
- changed files;
- unified diff;
- commits;
- lint/test/build results;
- coverage при наличии;
- reviewer verdict;
- QA verdict;
- security/risk verdict при необходимости;
- generated artifacts;
- известные ограничения/риски;
- фактическая стоимость.

---

## Milestone 7 — Approval-bound Git actions

Approval должен быть привязан к конкретному действию:
- task id;
- repository id;
- action;
- branch;
- commit SHA;
- destination;
- requester;
- approver;
- expiration;
- consumed_at.

Разрешение одноразовое.

Policy:
- прямой push в protected default branch запрещен;
- обычный путь: feature branch -> push -> PR -> CI -> review -> merge;
- merge и deploy — отдельные approvals/policies.

---

## Milestone 8 — Provider / CI integrations

Webhooks вместо постоянного polling для событий:
- PR created/updated;
- CI passed/failed;
- review comment;
- merge;
- issue/task event.

Поддержка поэтапно:
1. GitHub;
2. GitLab;
3. generic Git без PR API.

---

## Milestone 9 — FinOps

Автоматически собираем:
- input/output/cache tokens;
- provider/model alias;
- role;
- execution/task/project;
- duration;
- calculated/provider cost.

Guardrails:
- task budget;
- project monthly budget;
- global monthly budget;
- warning thresholds;
- hard stop на Model Router/worker уровне.

---

## Milestone 10 — Project Context / Memory

При регистрации/refresh repo индексируем безопасные метаданные:
- README;
- AGENTS.md;
- architecture/docs;
- package manifests;
- CI workflows;
- test commands;
- build commands;
- migrations;
- API contracts;
- coding conventions.

Project memory хранит подтвержденные conventions и команды, но не секреты.

---

## Milestone 11 — Database migrations

Перейти с одного `Base.metadata.create_all()` на Alembic:
- versioned schema;
- upgrade/downgrade policy;
- production migration preflight;
- backup before destructive migration.

---

## Milestone 12 — Notifications / RBAC / DR

### Notifications
- waiting approval;
- ready for review;
- QA failed;
- CI failed;
- budget threshold;
- execution stalled.

### RBAC
- Admin;
- Manager;
- Operator/Developer;
- Observer;
- Auditor.

### Backup / DR
- Control Plane PostgreSQL;
- audit trail;
- repository registry;
- approvals;
- execution metadata;
- artifact metadata;
- configuration without provider secrets.

---

## Domain isolation

Development, Analytics и Trading используют общий Control Plane, но разные execution policies/capabilities.

### Development
Может менять файлы в task worktree, запускать тесты и готовить Git changes.

### Analytics
Может читать разрешенные data sources и создавать отчеты/artifacts. Не получает возможности изменения production code автоматически.

### Trading
Research/modeling контур физически отделен от financial execution. AI Orchestra не получает прямой capability на проведение сделок.

---

## Ближайший порядок реализации

1. Закрыть Execution Progress V1.1.
2. Добавить `ripgrep` в OpenCode image и toolchain smoke.
3. Repository Registry schema/API.
4. Repo Manager service skeleton.
5. Shared repository/worktree storage.
6. Register/clone/fetch public repository.
7. Create/remove task worktree.
8. Связать Task с repository_id вместо свободного project string.
9. Workspace preflight перед LLM inference.
10. Перенести lifecycle в worker/job queue.
11. Multi-agent child executions.
12. Result package + diff/tests.
13. Scoped approval для push/PR.
14. Automatic FinOps.

## Definition of Done для «виртуального отдела разработки»

Система считается достигшей базового целевого состояния, когда руководитель может:

1. добавить ранее неизвестный Git repository через UI/API;
2. поставить development-задачу;
3. закрыть браузер;
4. Orchestra самостоятельно подготовит isolated worktree;
5. Lead сформирует план;
6. независимые роли выполнят architecture/coding/review/QA;
7. tests/build выполнятся в sandbox;
8. после возвращения в кабинет будет виден полный timeline и пакет результата;
9. без approval никакие изменения не уйдут наружу;
10. после approval будет создан push/PR с точной привязкой к проверенному commit SHA.
