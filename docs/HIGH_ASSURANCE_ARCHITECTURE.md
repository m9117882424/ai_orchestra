# AI Orchestra — High-Assurance Architecture

Статус: обязательная архитектурная политика для разработки Execution Platform V2.

## 1. Цель надежности

AI Orchestra должен быть пригоден для разработки и сопровождения дорогостоящих коммерческих проектов без зависимости от открытого браузера, единичного LLM-ответа или ручного восстановления внутреннего состояния.

Это не заявление о сертификации safety-critical ПО. Цель — high-assurance software engineering: fail-closed capabilities, воспроизводимые сборки, durable workflows, изолированное исполнение недоверенного кода, проверяемые артефакты, восстановление после отказов и полная трассируемость решений.

## 2. Неприкосновенные инварианты

Ни один новый feature не может нарушать эти правила.

1. **Browser is not the worker.** Закрытие браузера не влияет на execution lifecycle.
2. **Default branch is never an agent workspace.** Агент работает только в task-specific worktree/snapshot.
3. **No secret in agent context.** Git/provider/deployment credentials никогда не передаются LLM/OpenCode, prompt, tool output или task artifact.
4. **External side effects are idempotent and approval-bound.** Push/PR/merge/deploy выполняются отдельными activities с operation key и одноразовым approval, связанным с exact commit SHA.
5. **Approval becomes stale when the reviewed input changes.** Новый commit/diff инвалидирует старое разрешение.
6. **AI output is untrusted input.** Любой structured plan/tool request проходит schema validation и policy enforcement вне модели.
7. **Repository contents are untrusted input.** README/AGENTS/package scripts не могут расширить capabilities агента.
8. **Execution is reproducible.** Для задачи сохраняются base SHA, head SHA, repository id, workflow version, prompt/policy version, exact provider/model identity, tool image digest и test commands/results.
9. **No silent model drift.** Изменение underlying model/provider требует qualification suite и versioned Model Registry update.
10. **No direct autonomous production deploy.** Production deploy остается отдельным capability и human approval gate.
11. **Independent verification.** Coder не может быть единственным reviewer/QA своей работы.
12. **Failure must be explicit.** Unknown/stale/timeout state никогда не трактуется как success.

## 3. Trust boundaries

### Management Plane

Содержит:
- Nginx / ingress;
- Control Plane;
- durable workflow control;
- PostgreSQL metadata;
- Repository Registry;
- Approval Engine;
- Repo Manager credential broker;
- Audit/FinOps metadata.

Management Plane не исполняет код из пользовательских репозиториев.

### Execution Plane

Содержит ephemeral task sandboxes/runners.

Правила:
- нет production secrets;
- нет provider admin keys;
- нет Docker socket management plane;
- отдельная task filesystem;
- resource quotas;
- egress deny-by-default;
- sandbox уничтожается после сохранения результата.

Для high-value/private repositories целевой вариант — отдельный runner host/VM pool. Контейнер на том же host допустим только как development stage, не как окончательная security boundary для недоверенного кода.

### Provider Plane

Model Gateway/Router отделен от provider credentials. Агенты получают только inference client capability. Provider/admin credentials остаются вне execution plane.

### Git Credential Plane

GitHub App / GitLab token / SSH key хранит только Repo Manager. OpenCode и execution sandbox не видят credential material.

## 4. Arbitrary Git Repository Threat Model

Repository registration не означает автоматическое доверие.

### URL validation

Разрешены только явно поддерживаемые schemes/hosts.

Запрещено по умолчанию:
- `file://`;
- git external helpers / `ext::`;
- localhost;
- loopback/link-local/private metadata endpoints;
- URL с embedded credential;
- shell interpolation.

Для custom/self-hosted Git host требуется административная регистрация endpoint и network policy.

### Git process safety

Repo Manager:
- вызывает git без shell-конкатенации;
- запрещает hooks для автоматических операций;
- устанавливает `protocol.file.allow=never`;
- submodules выключены по умолчанию;
- Git LFS включается только profile/policy;
- применяет clone/fetch timeout;
- object/repository size quotas;
- sanitizes branch/worktree names;
- не сохраняет token в remote URL/config/log.

### Untrusted build scripts

`npm install`, `pip install`, `make`, test runner, compiler plugins и repository scripts считаются arbitrary code execution.

Они исполняются только в Execution Plane sandbox после workspace provisioning.

## 5. Durable Workflow Engine

### Решение

Предпочтительный workflow engine для V2: **Temporal**.

Причины:
- durable workflow state;
- restart/crash recovery;
- retries/timeouts;
- long-running waits for human approval;
- activity heartbeats;
- workflow history/auditability;
- естественная модель для AI-agent orchestration.

До production adoption выполняется PoC на наших failure scenarios. Самописная PostgreSQL/Redis queue допускается только как временный prototype и не является целевой архитектурой, если не предоставляет эквивалентные гарантии durable execution.

### Workflow rule

Workflow code координирует состояние. Любой side effect выполняется Activity.

Activities должны быть idempotent либо использовать deduplication/operation keys.

## 6. Database Integrity

До добавления Repository Registry и новых production tables:

1. внедрить Alembic;
2. создать baseline migration текущей схемы;
3. запретить `Base.metadata.create_all()` как production migration mechanism;
4. добавить schema-version preflight;
5. backup перед destructive migration;
6. migration rollback/forward-fix runbook.

Критичные state transitions выполняются транзакционно.

Для side effects применяется transactional outbox либо durable workflow activity state, чтобы commit DB и внешний action не расходились молча.

## 7. Authentication / Authorization

Текущий HTTP Basic является bootstrap-only.

До выдачи доступа другим сотрудникам или включения push/PR capabilities:
- OIDC/SSO либо локальная hardened authentication;
- MFA для privileged users;
- short-lived session;
- Secure + HttpOnly + SameSite cookie;
- CSRF protection;
- RBAC;
- brute-force/rate limiting;
- security event audit;
- session revocation.

Roles minimum:
- Admin;
- Manager;
- Operator/Developer;
- Observer;
- Auditor.

Для critical actions архитектура поддерживает four-eyes approval (requester != approver; при необходимости два approver).

## 8. Secrets

`.env` с mode 600 остается допустимым bootstrap механизмом, но не конечной архитектурой high-assurance.

Цель:
- dedicated secret manager или OS-backed secret injection;
- secret rotation;
- least privilege credential profiles;
- no secret in Git/DB/audit/task artifacts;
- secret scanning в CI и перед push;
- GitHub App installation tokens вместо long-lived PAT, где возможно;
- deployment credentials через short-lived identity/OIDC, где поддерживается.

## 9. Reproducible Builds / Supply Chain

Текущие dependency ranges заменяются lock files / exact resolved versions.

Требования:
- Python lock with hashes;
- Node lock;
- Docker base images pin by digest для release builds;
- GitHub Actions pin to reviewed immutable commit SHA;
- SBOM для release images/artifacts;
- vulnerability scan;
- dependency/license scan;
- secret scan;
- SAST;
- signed/attested release artifacts.

Цель supply-chain maturity: SLSA Build L2 как ближний target, L3 для критичных release paths при наличии подходящего hosted/hardened builder.

## 10. Model Governance

Model alias — не доказательство воспроизводимости.

Model Registry хранит:
- logical role;
- exact provider;
- exact model id/version;
- allowed repository sensitivity classes;
- qualification suite result;
- context/token constraints;
- fallback chain;
- effective_from/effective_to.

Fallback допускается только на заранее квалифицированную модель.

Execution сохраняет фактическую модель каждого child-run.

Изменение каталога агрегатора не должно автоматически менять production behavior.

## 11. Data Classification / Provider Policy

Каждый repository получает sensitivity class, например:
- public;
- internal;
- confidential;
- restricted.

Для каждого class задается provider allowlist.

До юридической/безопасностной проверки data retention, training use, subprocessors и contractual terms агрегатор моделей считается test/integration provider, а не автоматически разрешенным каналом для confidential/restricted source code.

## 12. Independent QA and Security Gates

Минимальная цепочка для code changes:

`Lead -> Architect (если нужен) -> Coder -> static/tests -> Reviewer -> QA -> Lead synthesis`

High-risk/security:

`Lead -> Architect -> Coder -> SAST/tests -> Reviewer -> Security/Risk -> QA -> Lead synthesis`

Reviewer получает clean context: task + diff + relevant repository context + test artifacts, а не chain-of-thought Coder.

Human approval остается последним gate для critical external action.

## 13. CI/CD Quality Gates

До merge Orchestra core:
- formatting/lint;
- type checks;
- unit tests;
- integration tests;
- API contract tests;
- Docker build;
- static security boundary tests;
- dependency vulnerability scan;
- secret scan;
- migration test;
- sandbox escape policy tests where applicable.

Для release:
- staging deploy;
- smoke;
- end-to-end task execution;
- rollback test or validated rollback artifact;
- immutable image digest recorded.

Нельзя выкатывать production непосредственно из working tree сервера.

## 14. Observability

Минимум:
- structured JSON logs;
- correlation IDs task/execution/child-run/activity;
- metrics;
- distributed traces where practical;
- provider latency/error metrics;
- queue/workflow lag;
- runner utilization;
- disk/storage quotas;
- alerts on stale workflow, repeated retry, DB failure, provider outage and budget threshold.

Audit trail должен быть append-oriented и экспортироваться off-host. Для high assurance добавляется tamper-evident chaining/signing либо WORM-capable storage.

## 15. Backup / Disaster Recovery

Back up:
- PostgreSQL metadata/history;
- repository registry;
- approvals;
- audit;
- workflow persistence where self-hosted;
- result/artifact metadata and non-reconstructable artifacts;
- configuration excluding plaintext secrets.

Не требуется backup disposable worktrees и git mirrors, если они reconstructable from remote + recorded SHA.

Initial target:
- encrypted off-host backup;
- daily full + incremental/WAL strategy appropriate for PostgreSQL;
- regular automated integrity check;
- documented restore procedure;
- monthly restore drill.

RPO/RTO устанавливаются до production use и проверяются drill, а не только записываются в документ.

## 16. Release / Rollback

Каждый Orchestra release имеет:
- semantic/version identifier;
- Git commit SHA;
- image digest;
- DB migration version;
- changelog;
- compatibility notes;
- rollback/forward-fix plan.

Production deploy идет staging -> validation -> production.

Rollback должен восстанавливать приложение без отката уже небезопасно примененной irreversible DB migration; поэтому destructive migrations используют expand/migrate/contract pattern.

## 17. Failure Injection Tests

До high-value repository enablement должны автоматически/регулярно проверяться сценарии:
- worker killed mid-task;
- Control Plane restart;
- Temporal/service restart;
- PostgreSQL reconnect/outage;
- provider 429/500/timeout;
- Model Gateway/Router restart;
- Git remote unavailable;
- Git credential expired;
- disk full/quota hit;
- malicious/oversized repository;
- task timeout;
- duplicate webhook;
- duplicate activity delivery;
- approval arrives after commit changed;
- runner dies during tests;
- backup restore into clean environment.

Success criterion — система либо продолжает корректно, либо останавливается в явном recoverable/failed state. Никогда не создает скрытый partial success.

## 18. Maturity Gates

### G0 — Lab

Разрешено:
- model smoke;
- read-only demo tasks;
- public test repositories.

Запрещено:
- private high-value source;
- push;
- deploy.

### G1 — Durable Core

Необходимы:
- Alembic;
- backups + successful restore drill;
- durable workflow PoC;
- execution state survives restart;
- exact dependency locks.

### G2 — Repository Safe

Необходимы:
- Repository Registry/Repo Manager;
- URL/Git hardening;
- task worktrees;
- no Git credentials in agent;
- workspace preflight;
- resource quotas.

После G2 можно подключать private repositories для read/edit only в соответствии с provider data policy.

### G3 — Sandbox Safe

Необходимы:
- isolated execution plane;
- egress policy;
- untrusted build/test execution sandbox;
- dependency/security scans;
- artifact integrity.

### G4 — Git Write Safe

Необходимы:
- scoped one-time SHA-bound approvals;
- protected default branch;
- human-reviewed result package;
- push/PR activities are idempotent;
- webhook signature/replay protection;
- approval invalidation on new commit.

После G4 допускается controlled push/PR.

### G5 — Production Delivery Safe

Необходимы:
- staging environment;
- release provenance/SBOM;
- canary/health validation where project supports it;
- tested rollback;
- deployment-specific approval policy;
- DR targets tested.

Только после G5 Orchestra может участвовать в production deployment workflow. Автономный production deploy по умолчанию остается запрещен.

## 19. Architecture Review Rule

Перед каждым новым capability отвечаем на пять вопросов:

1. Что произойдет, если процесс упадет ровно после side effect?
2. Может ли недоверенный repository/prompt расширить свои permissions?
3. Какие secrets потенциально окажутся в agent/model/log?
4. Можно ли доказать, какой exact code/model/environment дал результат?
5. Можно ли восстановить систему на чистой машине без ручной догадки?

Если хотя бы на один вопрос нет проверяемого ответа — capability не считается production-ready.
