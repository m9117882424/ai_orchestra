# G3.1 — Disposable Runner Boundary

Статус: implementation slice для G3. Не является разрешением на production rollout или high-assurance use.

## Цель

Недоверенные `install/build/test` команды не выполняются в Management Plane, OpenCode container или authoritative task workspace. Единственный компонент с доступом к host Docker API — узкий host-side `runnerd`; Docker socket не передается агентам и контейнерам.

## Request contract

`runnerd` принимает только Unix-socket JSON protocol v1 и валидирует:
- canonical `request_id`, `workspace_id`, `execution_id`;
- immutable `base_commit`;
- exact `preflight_digest`;
- argv как массив строк, без shell-конкатенации;
- bounded timeout.

Caller не может передать host path, volume name, image tag, network mode, mounts, user, capabilities или environment.

## Filesystem boundary

Authoritative named volume монтируется только как exact `volume-subpath=<workspace_id>` в `/source` с `readonly`.

Runner image проверяет `.git/ai-orchestra-workspace.json` против request binding до запуска команды. После проверки `/source` копируется в пустой одноразовый `/workspace` tmpfs. Build/test пишет только в disposable snapshot; изменения не возвращаются в authoritative task workspace.

## Runtime boundary

Каждый запуск использует exact local Docker image ID (`sha256:...`) и `--pull never`, а также:
- `network=none`;
- non-root `10001:10001`;
- read-only container root;
- `cap-drop ALL` и `no-new-privileges`;
- без Docker socket и host `/root`;
- CPU/RAM/PID/time/output limits;
- отдельные tmpfs для `/tmp`, `$HOME` и disposable `/workspace`.

`runnerd` передает только служебные identity variables и `HOME/CI/NO_COLOR`; provider, Git, DB и deployment secrets не наследуются.

После любого success/failure/timeout broker подтверждает отсутствие disposable container. Неоднозначный cleanup возвращает `cleanup_uncertain`, а не success.

## Host broker

`runnerd` запускается отдельным hardened systemd service. Socket имеет mode `0660` и отдельную группу `ai-orchestra-runner`. Будущий Runner Manager получает только этот Unix socket; `/var/run/docker.sock` в контейнеры не bind-mountится.

Service использует `PrivateNetwork`, `PrivateDevices`, `ProtectSystem=strict`, пустой capability bounding set и другие systemd sandbox controls. Root нужен только как узкий broker к host Docker daemon; untrusted code root не получает.

## Evidence gates

`runner/tests` проверяет protocol validation, immutable binding, command construction, bounded output и fail-closed cleanup.

`scripts/runner-isolation-smoke.sh` выполняет реальный Docker test:
1. строит runner image и фиксирует image ID;
2. создает два соседних workspace в одном volume;
3. подтверждает доступ только к exact subpath;
4. подтверждает read-only authoritative source и writable disposable snapshot;
5. подтверждает отсутствие Docker socket, secrets и network egress;
6. проверяет manifest mismatch до command execution;
7. проверяет timeout и подтвержденный container cleanup;
8. подтверждает отсутствие изменений исходных workspace.

## Ограничения этого slice

- Durable `runner_jobs`, lease/fencing/retry и связь с execution появятся в G3.2.
- Artifact export из disposable snapshot относится к G4 evidence/result package.
- Runner toolchain image использует pinned base digest и runtime admission по exact image ID, но apt tool packages пока не зафиксированы как полностью reproducible supply-chain baseline; это обязательный follow-up перед controlled/high-assurance use.
- Same-host Docker container — development-stage security boundary. Для high-value/private проектов целевой профиль использует отдельный runner host/VM pool.
- G3.1 не предоставляет Git credentials, push/PR/deploy capabilities или provider credentials.
