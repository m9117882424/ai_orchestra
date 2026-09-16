# G3.2 — Durable Runner Jobs

Статус: **production accepted 2026-09-16**.

## Цель

Связать доказанную G3.1 sandbox boundary с durable Control Plane state без передачи Docker capability агентам или execution-worker.

Контур:

`Control Plane -> runner_jobs -> Runner Manager -> runnerd Unix socket -> disposable runner`

Runner Manager — отдельный сервис. Он имеет доступ только к PostgreSQL и Unix socket `runnerd`; у него нет Docker socket, task-workspace volume, Git credentials, provider/model credentials или deployment secrets.

## Durable job contract

Control Plane принимает от manager только:
- `idempotency_key`;
- `argv` как массив строк;
- bounded `timeout_seconds`.

Клиент не передает repository/workspace/base/preflight identity. Эти поля копируются сервером из уже verified execution contract v2.

Перед enqueue обязательны:
- execution contract v2;
- immutable repository/workspace binding;
- workspace preflight evidence;
- runtime preflight digest, совпадающий с durable digest;
- repository в trusted `ready` state;
- workspace в `ready` state.

Повтор того же `(execution_id, idempotency_key)` с тем же payload возвращает существующий job. Изменение argv/timeout при том же ключе отклоняется.

## Lease и fencing

`runner_jobs` хранит generation, owner, expiry, heartbeat и retry state.

Claim выполняется PostgreSQL `FOR UPDATE SKIP LOCKED`. Только владелец точной незавершенной generation может записать terminal result.

Lease длиннее максимального runner timeout плюс response/cleanup padding. Это важно: потеря ответа после отправки команды не приводит к немедленному повторному sandbox запуску.

После expiry новый Runner Manager может получить новую generation. Старый manager больше не может зафиксировать результат.

Перед Unix-socket side effect Manager повторно сверяет execution, workspace и repository trust.

## Terminal evidence

DB constraints не позволяют объявить `completed`, `failed` или `timed_out` без:
- exact runner image identity;
- подтвержденного cleanup;
- terminal timestamp;
- согласованного exit-code state.

`cleanup_uncertain` является отдельным fail-closed terminal state. `rejected` используется, когда side effect не был разрешен, например после repository trust revocation.

stdout/stderr bounded и сохраняются как evidence текущего slice. Artifact export и полноценный result package относятся к G4.

## Runtime deployment boundary

Runner Manager поставляется отдельным opt-in Compose overlay `deploy/docker-compose.runner-manager.yml`.

Обычный production `docker compose up` его не включает. Overlay требует существующий host socket и host group GID; `create_host_path: false` запрещает тихо создавать каталог вместо socket.

Сам `runnerd` остается единственным компонентом с host Docker API capability.

## Evidence gates

Unit/API/schema tests проверяют idempotency, immutable binding, trust recheck, lease generation и stale-owner rejection.

`scripts/postgres-schema-smoke.sh` проверяет constraints непосредственно в PostgreSQL и schema-drift refusal.

`scripts/runner-manager-durable-smoke.sh` выполняет реальный E2E:
1. мигрирует изолированный PostgreSQL до `20260916_0008`;
2. создает immutable execution/workspace binding;
3. запускает Runner Manager через Unix socket;
4. выполняет disposable job;
5. симулирует crash Manager после старта sandbox;
6. подтверждает отсутствие немедленного duplicate retry;
7. восстанавливает job новой lease generation;
8. проверяет trust revocation до runner side effect;
9. подтверждает неизменность authoritative workspace и runtime isolation Manager.

## Production acceptance 2026-09-16

- merge/main SHA: `1496caf717bbf06cbacb2437ba931e227711a508`;
- schema: `20260916_0008`;
- host `runnerd` active, Unix socket `root:ai-orchestra-runner` mode `0660`;
- Runner Manager container healthy и имеет только `control-db` + read-only runnerd socket;
- pinned disposable image: `sha256:4eefe1580c38fdc3a536e9eb1f8a25adc55d8703675da376c68b6de3d55f06d2`;
- acceptance execution: `5d612636-3e75-43a9-b1ac-e67230925753`;
- acceptance workspace: `a63a6dae-3ff9-41d1-ae85-d6350da4dfe9`, retained, `changed_file_count=0`;
- acceptance runner job: `6a24b32e-cb65-4a0f-91b4-07c2d9cbe4e9`, `completed`, exit `0`, cleanup confirmed;
- audit lifecycle: `queued -> lease_claimed -> authorized -> completed`;
- post-rollout verified backup: `ai-orchestra-20260916T114807Z.tar.gz`, SHA-256 `4ae77d6a3b532135e87c44810e8a6880b3d8bc97db3472a02edb478d9aebc8b4`;
- full production model/runtime smoke: green, Model Gateway `5/5`.

## Что остается в G3

G3.2 не включает автоматический multi-agent workflow. Следующий slice должен маршрутизировать install/build/test child-runs через durable Runner Jobs и связать результаты с Reviewer/QA/Lead workflow.

Git push, PR, deploy, production credentials и financial actions остаются вне G3 Runner Manager и требуют последующих approval/capability gates.
