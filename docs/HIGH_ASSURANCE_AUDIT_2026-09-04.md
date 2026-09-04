# AI Orchestra — High-Assurance Gap Audit

Дата: 2026-09-04

Цель: зафиксировать текущие gaps до перехода от Lab/MVP к платформе, которой можно доверять работу с high-value commercial repositories.

## Executive decision

Текущий контур остается **G0 / Lab**.

Он пригоден для:
- smoke/integration tests моделей;
- публичных/тестовых read-only задач;
- развития самого Control Plane.

До закрытия G1–G3 не разрешаем:
- передачу confidential/restricted source code через непроверенные provider channels;
- исполнение недоверенных repository scripts в management plane;
- autonomous Git writes;
- production deployment.

---

## P0 — обязательные до high-value repositories

### P0.1 Database migrations отсутствуют

Текущее приложение вызывает `Base.metadata.create_all()` на startup. Это не обеспечивает контролируемую эволюцию production schema.

Action:
- Alembic baseline;
- migration tests;
- schema version preflight;
- expand/migrate/contract для destructive changes.

### P0.2 Execution lifecycle пока не durable

Execution V1 опирается на OpenCode session и browser/API refresh. Нет независимого durable workflow ownership, activity retries и recovery guarantee.

Action:
- Temporal PoC;
- restart/failure scenarios;
- browser only observes state;
- external side effects only as idempotent activities.

### P0.3 OpenCode сейчас совмещает agent runtime и Git workspace management

Существующий `worktree-create.sh` выполняет Git операции внутри OpenCode container. Для целевой модели это слишком широкая capability.

Action:
- отдельный Repo Manager;
- Git credentials и clone/fetch/push только там;
- OpenCode получает task workspace, но не credential capability.

### P0.4 Arbitrary repository code не изолирован от management host

Будущие `npm install`, `pip install`, `make`, test/compiler plugins являются arbitrary code execution.

Action:
- execution plane/sandbox;
- для high-value target — отдельный runner host/VM pool;
- deny-by-default egress;
- CPU/RAM/PID/time quotas;
- no Docker socket / no management secrets.

### P0.5 Authentication bootstrap-only

Текущий Control Plane использует HTTP Basic и статический mutation marker header.

Action до shared/privileged use:
- OIDC/SSO or hardened session auth;
- MFA;
- RBAC;
- CSRF;
- rate limiting;
- session revocation;
- privileged action policy.

### P0.6 GitHub main не защищен repository ruleset

На момент аудита repository rulesets отсутствуют.

Action:
- PR-only changes to main;
- required CI checks;
- block force push/delete;
- stale approval invalidation;
- require conversation resolution as appropriate;
- CODEOWNERS/security review for sensitive paths.

### P0.7 Backup существует, но DR не доказан

Есть локальный `scripts/backup.sh` с PostgreSQL dump/checksums, но нет доказанного off-host backup pipeline, restore utility и restore drill.

Action:
- encrypted off-host destination;
- restore script/runbook;
- automated integrity verification;
- restore into clean environment;
- define/test RPO/RTO.

### P0.8 Dependency/release reproducibility недостаточна

Current examples:
- Python requirements use version ranges;
- Docker base/service images use mutable tags;
- CI uses mutable runner/action/tool inputs;
- CI installs live packages;
- Docker validation uses `--pull`.

Action:
- dependency lock + hashes;
- pin Docker release bases by digest;
- pin CI actions by immutable SHA;
- stable runner policy;
- deterministic build definition;
- scheduled controlled dependency-update PRs instead of silent drift.

### P0.9 Model drift governance отсутствует

Уже наблюдались provider catalog model-name changes. Logical aliases могут скрыть изменение underlying model behavior.

Action:
- Model Registry;
- exact provider/model identity per execution;
- qualification suite before alias/fallback change;
- versioned fallback policy.

### P0.10 Confidential source/provider policy не формализована

Source code может быть отправлен внешнему provider/aggregator. Для дорогих proprietary projects это data governance вопрос, не только технический.

Action:
- repository sensitivity classification;
- provider allowlist per class;
- retention/training/subprocessor/legal review;
- aggregator channel считать test-only для confidential/restricted data до отдельного разрешения.

---

## P1 — обязательные до Git write / PR automation

### P1.1 Approval must bind exact side effect

Нужно хранить repository, branch, exact commit SHA, operation, expiration, requester/approver и consumed state.

Любой новый commit invalidates approval.

### P1.2 Idempotency / duplicate delivery

Webhook retry, workflow replay или worker restart не должны создать второй push/PR/deploy.

Нужны operation keys и idempotent activities.

### P1.3 Webhook security

- signature validation;
- replay window/idempotency;
- event delivery id dedupe;
- source/provider verification.

### P1.4 Audit tamper resistance

Current DB audit is useful but app-owned DB alone is not sufficient as final trust anchor.

Target:
- off-host append-only export;
- hash chaining/signing or WORM-capable storage for high assurance.

### P1.5 Supply-chain security

Add:
- secret scan;
- SAST;
- dependency vulnerability scan;
- license scan;
- image scan;
- SBOM;
- provenance/attestation.

Target: SLSA Build L2 first, L3 for critical release builders.

### P1.6 AI prompt injection from repository

Repository files are untrusted input. A malicious README/AGENTS can instruct an agent to exfiltrate data or bypass process.

Mitigation:
- no secrets in agent plane;
- egress limits;
- capability enforcement outside model;
- structured tool schemas;
- system policy cannot be overridden by repo content;
- security tests with malicious fixture repositories.

### P1.7 Independent QA must be enforced by orchestrator

Prompt-only delegation is insufficient. Child runs are separate persisted entities with independent reviewer/QA contexts and verdict schemas.

---

## P2 — reliability/operations maturity

### P2.1 Observability

Need structured logs, task/execution/activity correlation IDs, metrics, traces, alerts and provider health/cost dashboards.

### P2.2 Staging / immutable release / rollback

Before production delivery capability:
- staging environment;
- immutable image digests;
- migration version tracking;
- release manifest;
- tested rollback/forward-fix.

### P2.3 Failure injection

Regularly test:
- kill worker mid-task;
- DB outage/restart;
- provider timeout/429/500;
- Git remote unavailable;
- disk full;
- runner death;
- duplicate webhook/activity;
- stale approval;
- clean-environment restore.

### P2.4 Capacity protection

Per task/repo/user quotas:
- max repo size;
- max artifact size;
- max concurrent tasks;
- max CPU/RAM/time;
- max tokens/cost;
- workspace TTL/GC.

---

## Corrected implementation order

### G1 — Durable Core

1. Merge/fix Execution Progress V1.1.
2. Add Alembic baseline and remove production schema mutation via `create_all`.
3. Introduce dependency lock/reproducible CI foundations.
4. Harden OpenCode base toolchain (`ripgrep` etc.) and add toolchain smoke.
5. Implement off-host backup contract + restore command + restore test.
6. Temporal PoC for restart-safe execution + human approval wait.
7. Add execution heartbeat/timeout semantics independent of browser.

### G2 — Repository Safe

8. Repository Registry.
9. Repo Manager service with Git URL threat controls.
10. GitHub App/auth profiles without agent credential exposure.
11. task-specific worktree/snapshot lifecycle.
12. workspace preflight before any LLM inference.
13. repository sensitivity/provider allowlist policy.

### G3 — Sandbox Safe

14. Separate execution sandbox/runner boundary.
15. untrusted install/build/test isolation.
16. egress/resource policies.
17. SAST/secret/dependency/license scans.
18. content-addressed artifacts + integrity metadata.

### G4 — Git Write Safe

19. exact-SHA one-time approvals.
20. protected main/rulesets.
21. idempotent push/PR activities.
22. webhook security/dedupe.
23. CI feedback loop through durable workflows.

### G5 — Production Delivery Safe

24. staging and immutable releases.
25. SBOM/provenance.
26. deployment-specific approval.
27. rollback/canary/health gates.
28. tested DR objectives.

---

## Rule for all future work

Feature priority is subordinate to safety gates.

If a requested feature requires a capability from a later gate, we build the prerequisite gate first or keep the feature disabled/fail-closed.
