# AI Orchestra — Execution Platform V2.1 Roadmap

## Цель

Построить полноценный виртуальный отдел разработки, способный надёжно вести проекты разного масштаба — от обычных внутренних сервисов до дорогих business-critical систем — без привязки к конкретному репозиторию, стеку или отрасли.

Авиация, атомная энергетика, космос и другие safety-critical отрасли используются как **стресс-тест архитектуры надёжности**, а не как обязательный процесс для каждого проекта. Специальные нормативные требования включаются только через отдельный Domain Assurance Profile.

## Универсальный уровень надёжности ядра

Эти свойства обязательны для AI Orchestra независимо от проекта:

1. **Repository-agnostic** — подключается любой разрешённый Git repository.
2. **Durable execution** — закрытие браузера, restart worker/Control Plane и кратковременный outage не теряют задачу.
3. **Fail closed on ambiguity** — неизвестное состояние не считается успехом.
4. **One task = isolated workspace/sandbox** — параллельные задачи не портят друг друга.
5. **Secrets never reach agents** — Git/provider/production credentials отделены от AI execution plane.
6. **Content-addressed actions** — approvals и результаты привязаны к точным SHA/digest, а не только к mutable task/branch names.
7. **Idempotent external effects** — retry/duplicate delivery не создают два push, два релиза или две противоречивые истины.
8. **Observable execution** — фактические стадии, tool calls, tests, errors, retries, cost.
9. **Independent QA proportional to risk** — автор изменения не является единственным принимающим его результат.
10. **Recoverable and reconcilable** — после сбоя состояние можно восстановить и сверить с Git/artifacts/evidence.
11. **Supply-chain isolation** — код из repository и dependency scripts считаются недоверенными.
12. **Auditable material actions** — можно доказать что, кем/чем, когда и над каким immutable объектом было сделано.

## Assurance tiers

### `general-standard`
Обычная продуктовая/внутренняя разработка. Минимум бюрократии, но все базовые свойства надёжности ядра сохраняются.

### `general-high-assurance`
Целевой режим для дорогих, долгоживущих и business-critical проектов. Более строгие review, release, provenance, DR и failure-injection gates.

### `regulated-critical`
Опциональный режим для авиации, атомной энергетики, космоса, rail, medical и т.п. Подключает отдельные отраслевые профили и дополнительные evidence/independence requirements.

---

# G0 — Current Lab

Существуют Control Plane, Model Gateway/Router, OpenCode, execution V1 и базовые approvals/audit.

Ограничения:
- browser polling всё ещё участвует в lifecycle;
- нет общего durable workflow engine;
- нет полноценного repository lifecycle;
- execution workspace не гарантируется preflight;
- DB migration process ещё не production-grade;
- build/dependency reproducibility ограничена;
- Git write/release autonomy отключена.

---

# G1 — Durable Core

Это следующий обязательный этап.

### Database / state
- Alembic baseline;
- production startup не использует `create_all()` как migration mechanism;
- schema-version preflight;
- backup перед опасными migrations.

### Durable workflow
- Temporal или эквивалентный durable engine;
- worker lifecycle не зависит от браузера;
- heartbeat, timeout, retry, cancellation;
- recovery после worker/Control Plane restart;
- idempotency keys;
- fencing/generation semantics;
- reconciliation после uncertain external effects.

### Build/dependencies
- lock/exact dependency strategy;
- честное разделение `build succeeds` и `reproducible build`;
- pinned base/runtime versions для production images;
- toolchain smoke (`read/glob/grep/git/tests`).

### Backup / DR
- off-host backup contract;
- restore command/runbook;
- periodic clean-environment restore drill.

### Acceptance
- закрываем браузер во время execution — задача продолжается;
- рестартуем worker/Control Plane — задача восстанавливается;
- duplicate delivery не удваивает external effect;
- восстановленная БД не возрождает уже использованное approval/action;
- failure injection оставляет систему в понятном recoverable/fail-closed состоянии.

---

# G2 — Repository & Workspace Platform

### Repository Registry
Для любого Git repository:
- id/name/remote/provider;
- auth profile reference без секрета;
- default branch;
- enabled/status;
- last known commit/fetch;
- execution profile;
- assurance tier/profile.

### Repo Manager
Отдельный trusted service:
- register/validate;
- clone/fetch/prune;
- default branch detection;
- task branch/worktree;
- status/diff/tree identity;
- cleanup;
- prepare commit;
- push только через scoped policy/approval.

AI/OpenCode не получает Git credentials.

### Workspace preflight
До первого LLM inference:
- repo доступен;
- base SHA существует;
- worktree создан;
- expected files доступны;
- toolchain готов;
- workspace writable/read-only policy соответствует роли.

Если preflight не пройден — inference не запускается.

---

# G3 — Isolated Execution & Multi-Agent Department

### Execution sandbox
- disposable runner;
- no Docker socket;
- no host `/root`/`.env`;
- no management DB/control-plane network;
- CPU/RAM/PID/time limits;
- egress policy;
- explicit writable workspace;
- untrusted install/build/test scripts только здесь.

### Real orchestration
Не prompt-only delegation.

Базовый development workflow:

`Lead -> Architect (as needed) -> Coder -> Reviewer -> QA -> Lead synthesis`

Policy сокращает workflow для простых задач и усиливает для high-risk.

Каждый child-run имеет:
- role/model;
- status/start/end;
- input/output identity;
- tool calls;
- tests/artifacts;
- cost;
- error/retry lineage.

---

# G4 — Evidence, Result Package & Observability

### Live progress
- stage/role;
- elapsed time;
- heartbeat;
- actual tool calls;
- build/test status;
- retry/error state;
- token/cost metrics;
- никаких выдуманных `% готовности`.

### Result package
- original task;
- plan;
- base/head SHA;
- changed files/diff;
- commits/proposed commits;
- lint/test/build outputs;
- reviewer/QA verdicts;
- generated artifacts;
- known risks/limitations;
- actual cost.

### High-assurance projects
При выбранном профиле добавляются stronger provenance/evidence/traceability requirements. Они не навязываются обычному проекту.

---

# G5 — Controlled Git & External Actions

Approval связан с точным действием:
- task/repository;
- action;
- source/base/head SHA;
- destination;
- requester/approver;
- expiry;
- one-time consumption.

Правила:
- direct push в protected default branch запрещён;
- обычный путь: feature branch -> PR -> CI -> review -> merge;
- merge/deploy — отдельные policies;
- retry безопасен и не создаёт duplicate effect;
- external state reconciliation обязательна после uncertain response.

---

# G6 — High-Assurance Operations

Для самого AI Orchestra и проектов `general-high-assurance`:
- SBOM/provenance;
- immutable release manifest;
- failure-injection/chaos suite;
- backup/restore/reconciliation drills;
- incident/CAPA process;
- model/tool configuration baseline and drift detection;
- stronger RBAC/MFA/session security;
- branch/ruleset enforcement;
- operational SLOs and alerting;
- capacity/backpressure controls.

Это инженерная надёжность, а не отраслевой certification process.

---

# Optional Domain Assurance Profiles

Только `regulated-critical` проекты подключают дополнительные профили из `docs/DOMAIN_ASSURANCE_PROFILES.md`.

Примеры:
- aviation;
- nuclear digital I&C;
- space;
- rail;
- medical;
- customer-specific regulated profile.

Core не содержит DAL/MC/DC/FHA/PSSA/nuclear licensing logic по умолчанию.

---

# Domain isolation

Development, Analytics и Trading используют общий Control Plane, но разные capabilities/policies.

### Development
Работает с task worktree, code/build/test и контролируемыми Git changes.

### Analytics
Читает разрешённые data sources и создаёт reports/artifacts; не получает автоматически права менять production code/data.

### Trading
Research/modeling физически и политически отделён от financial execution. Автоматическое проведение сделок не наследуется из development capabilities.

---

# Ближайший порядок реализации

1. Закрыть Execution Progress V1.1.
2. Alembic baseline + migration preflight.
3. Dependency/build reproducibility hardening.
4. OpenCode toolchain (`ripgrep` и smoke).
5. Backup/restore drill.
6. Temporal durable-workflow PoC.
7. Idempotency/fencing/reconciliation foundation.
8. Repository Registry.
9. Repo Manager.
10. Task worktrees + workspace preflight.
11. Isolated execution runner.
12. Real multi-agent child executions.
13. Result package/evidence.
14. Scoped Git push/PR approvals.
15. FinOps + notifications + operational hardening.

## Definition of Done для базового «виртуального отдела разработки»

Руководитель может:

1. добавить ранее неизвестный Git repository;
2. выбрать подходящий assurance tier;
3. поставить development-задачу;
4. закрыть браузер;
5. Orchestra подготовит isolated workspace;
6. профильные роли выполнят работу и независимую QA по policy;
7. tests/build выполнятся в sandbox;
8. после возврата доступен полный timeline и result package;
9. сбои/retries не повреждают Git/state и не создают duplicate effects;
10. без разрешения никакое защищённое внешнее действие не выполняется;
11. после разрешения push/PR относится к точно проверенному immutable состоянию;
12. восстановление после сбоя/backup подтверждено практическим drill, а не только документацией.
