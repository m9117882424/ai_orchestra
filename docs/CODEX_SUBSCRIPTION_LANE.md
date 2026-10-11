# Codex subscription lane — stage 4 runbook

## Issue 49 container staging profile (opt in)

The container files are `deploy/codex-worker/Dockerfile` and
`deploy/docker-compose.codex-subscription.yml`. The base Compose file is unchanged.
The overlay must be named explicitly with `-f`; its worker also requires
`--profile codex-subscription`. No service was started or credential provisioned
by this repository change. Use this only against an isolated staging CP and test
database; this is not a production release instruction. Always name the target
service in `up`/`create`: a bare Compose `up` includes the existing base services.

**Trust boundary:** The subscription worker reaches the broker at exactly
`http://control-plane:8080` on a dedicated internal `codex-control` Docker bridge.
This is enabled only by the explicit `--isolated-control-network` option; without
it the worker accepts only the exact loopback origin. The code rejects every
other host, port, path, userinfo, redirect and proxy. No sidecar TCP relay sees
the bearer token. Control Plane independently validates every bearer request.
Only the trusted worker and Control Plane may join the internal bridge. Docker
host operators can still inspect bridge traffic and mounted files; securing the
host is a prerequisite. The separately attached `codex-egress` bridge allows
ChatGPT authorization and inference, but has no outbound IP allowlist, so high-
assurance deployments require a reviewed egress policy before production.

The worker image uses Python 3.12, Git, the `control_plane/requirements.lock`
with pip hash checking, and official `@openai/codex@0.156.1` from npm. It runs as
`10001:10001` with a read-only root, no capabilities, no new privileges, 128
processes and 4 GiB memory maximum. Compose mounts the existing named G2 volume
read-only at `/workspace/worktrees/managed`, an independent writable HOME volume,
a separate results volume, and a writable one-shot token directory. It has no DB,
manager credential, Docker socket, model router network, provider token or paid
API environment variable. The worker attaches directly to the dedicated
`codex-control` bridge shared with CP and the separate `codex-egress` bridge.
The CP image remains Git-free.

### Exact staging setup

1. Work from the repository root in a staging Compose project with synthetic or
   staging `.env`, `.env.providers`, and `.env.repositories` outside Git. Prepare
   a **test** database at the stage 4 migration. Do not reuse production paths
   or credentials. Set `CODEX_CP_TOKEN_FILE` to an absolute external regular
   file and `CODEX_WORKER_TOKEN_DIR` to an absolute external directory. Use a
   32+ character random printable token without newline. Provision the CP file
   mode `0400`, owner UID 10001; provision a separate file with the same value
   at `$CODEX_WORKER_TOKEN_DIR/one-shot.token`, mode `0400`, owner UID 10001.
   The worker directory must be `0700`, owner UID 10001, with no symlink in its
   path. An operator controls both external locations; never put token bytes in
   Compose environment, Git, logs, prompts, or a Docker image. Reprovision the
   one-shot copy for **every** worker start because startup unlinks it.
2. Set `CODEX_PREFLIGHT_WORKSPACE_ID` to an already prepared G2 UUID. Build with:

   ```bash
   docker compose -f docker-compose.yml -f deploy/docker-compose.codex-subscription.yml --profile codex-subscription build codex-subscription-worker
   ```

   The first build needs network access to fetch pinned packages. The Dockerfile
   specific ignore file restricts the worker build context to the lock and worker
   source files. The base file alone does not build or start the profile.
3. Authorize **this** UID 10001 HOME volume using `codex login --device-auth`
   in a one-off container built from the worker image. For the default project
   name:

   ```bash
   docker volume create ai-orchestra-codex-subscription-home
   docker volume create ai-orchestra-codex-subscription-results
   docker run --rm -it --user 10001:10001 --entrypoint codex -e HOME=/home/orchestra -e CODEX_HOME=/home/orchestra/.codex -v ai-orchestra-codex-subscription-home:/home/orchestra ai-development-department/codex-subscription-worker:0.156.1 login --device-auth
   docker run --rm --user 10001:10001 --entrypoint codex -e HOME=/home/orchestra -e CODEX_HOME=/home/orchestra/.codex -v ai-orchestra-codex-subscription-home:/home/orchestra ai-development-department/codex-subscription-worker:0.156.1 login status
   ```

   Confirm ChatGPT
   authorization. Use device authorization directly; **never copy any
   `auth.json`** from a host or another identity. Keep this volume private.
4. On staging only, recreate CP with the overlay so it receives the read-only
   token mount and `CONTROL_PLANE_CODEX_WORKER_TOKEN_FILE` setting. Create
   the worker without starting it:

   ```bash
   docker compose -f docker-compose.yml -f deploy/docker-compose.codex-subscription.yml --profile codex-subscription up -d --no-deps --force-recreate control-plane
   docker compose -f docker-compose.yml -f deploy/docker-compose.codex-subscription.yml --profile codex-subscription create --no-deps codex-subscription-worker
   ```

   Prepare a **second** G2 workspace after container creation, verify its
   permissions and clean status through a one-off UID 10001 container using the
   same read-only G2 volume, then update `CODEX_PREFLIGHT_WORKSPACE_ID` to that
   new UUID and recreate the stopped worker container. This confirms that volume
   visibility is not limited to workspaces present when the container was made.
   The startup preflight itself checks the selected workspace before claiming.
   Run `docker compose ... start codex-subscription-worker` only after both
   checks pass and an authorized operator accepts this staging bridge trust boundary.

The startup preflight checks UID/GID 10001, read-only root and G2 mount via
`statvfs`, G2 owner/modes `0700` and manifest `0400`, readable manifest, clean
Git status including ignored files, separate HOME mount, private `0700` HOME,
CODEX_HOME, results and token directories, `0400`/`0600` one-shot token file,
missing Docker socket and forbidden token environment variables. It confirms
`codex login status` reports ChatGPT before the broker can claim. The operator
must additionally verify that the HOME volume was separately device-authorized;
filesystem checks cannot prove a credential was never copied. A failed preflight
exits without consuming the token or claiming work. The worker's own G2 binding
verification still runs before and after each role. Inspect the generated model
prompt/evidence in staging to confirm the token pathname is absent from model
context; the worker's subprocess environment allowlist omits token variables.

### CI, failure, and rollback

`scripts/codex_compose_synthetic_check.py` renders the overlay with synthetic
paths and checks mounts, identity, limits, networks, profile and absence of
worker secrets. CI runs it after creating synthetic configuration files. It
does not need a Docker daemon or build/pull an image. Build, device login,
read-only volume behavior, and broker execution require a staging runtime and
were not exercised by this change.

If the token directory is read-only, the token cannot be unlinked and the worker
fails before a claim. If login, G2 permissions, or any preflight fails, it exits.
After a claim, lease and cancellation behavior remains as documented below;
there is no automatic paid API or OpenCode fallback. Preserve results and DB
history for inspection. To roll back in staging, stop the worker,
recreate CP from the base Compose file without the overlay to remove the token
mount/setting, and retain volumes and evidence. Do not remove database rows to
force a migration downgrade.

## Supported contract

`POST /api/tasks/{task_id}/execute` defaults to `{"backend":"codex-subscription"}` for development tasks. The request also accepts `{"backend":"opencode"}` only when the server has `CONTROL_PLANE_ALLOW_PAID_OPENCODE_EXECUTION=true`; without both conditions it returns 403. Unknown backends return 422. There is no fallback between providers. Existing rows migrate to `backend=opencode` and keep their existing owner. Old failed runs are not replayed.

The Codex path preserves repository readiness, manager Basic + mutation header, task lock, workspace-manager G2 preflight, immutable base commit/tree, and the existing approval and CapabilityGuard boundaries. `preparing` becomes `queued` only after G2 prepares the managed workspace. The broker claims only queued subscription rows. The OpenCode worker claims only OpenCode rows.

Worker API uses `Authorization: Bearer <private token>` and only `POST /api/codex-worker/claim`, `POST /api/codex-worker/{run_id}/preflight` with `generation`, `base_commit`, `base_tree`, and `workspace_preflight_digest`, `POST /api/codex-worker/{run_id}/heartbeat` with `generation`, and `POST /api/codex-worker/{run_id}/phase` with `generation`, `role`, `outcome`, `model`, `final_text`, `final_sha256`, `events_sha256`, `artifact_path`, `base_commit`, `base_tree`, and `workspace_preflight_digest`. It exposes no arbitrary command field. `role` is architect, qa or reviewer in that order; outcome is completed, execution_failed, local_timeout, incomplete_result, read_only_violation, auth_failed or workspace_failed.

The worker performs `architect → qa → reviewer`, each as a separate read-only Codex invocation and separate `execution_child_runs`/`execution_evidence` row. The CP verifies the G2 database binding and repository trust at claim, preflight acknowledgement and result acceptance; it has neither Git nor the managed workspace mount. Claim keeps the run `queued` in `codex_preflight_pending` with a fenced lease. The trusted worker verifies the G2 manifest, commit, tree, branch and clean status in its read-only G2 mount, then acknowledges the exact immutable source commit, tree and preflight digest. Only that acknowledgement atomically records runtime verification and moves the run to `running` / `architect_running`; no Codex inference starts before it succeeds. CP rejects stale generations, expired leases, changed trust or binding, and missing, extra or mismatched attestation fields. The worker repeats verification before and after each role. A role requires process success, `turn.completed`, nonempty final report and unchanged workspace. On failure the run fails; the task is never marked done. Successful review leaves the task in `qa` for the existing human process. Terminal runs request G2 inspection, so the Result Package is provisional until that inspection completes. Subscription usage and cost are `unknown`, including the G4 cost summary, not zero rubles. Artifacts are private files on the worker host; the CP stores their path, checksums, report up to 100,000 Unicode characters and 400,000 UTF-8 bytes, and immutable base identifiers. Larger reports fail closed and remain in the private artifact file. CP verifies the report checksum but cannot independently verify the private events checksum or repeat the filesystem check. Preserve worker evidence directories for inspection; restrict DB and artifacts because reports may contain private data. The worker also imposes prompt and event-size limits; artifact size and total quota are enforced as described in the staging policy.

## Isolated staging setup

1. Apply additive Alembic revision `20261010_0012` to a **test** database only. Deploying to production requires a separate release decision. Generate a random 32+ character printable ASCII token without whitespace or newline outside the repository. Keep the CP master token in a root-admin controlled regular private file and set `CONTROL_PLANE_CODEX_WORKER_TOKEN_FILE` to its CP-local path. Never pass the master path to the worker. An operator separately provisions one ephemeral worker-readable copy under `/run/codex-subscription-private`, owned by UID 10001 with mode `0700`; the copy must be a regular file owned by UID 10001, mode `0400` or `0600`, with no symlink in its path. Provision it immediately before each worker start using a privileged operator procedure outside this repository. The worker reads it once, unlinks the copy before any Codex subprocess, and refuses to start if unlink fails. A restart needs a newly provisioned copy. Do not put the token in `.env`, Git, DB, prompt or logs.
   `schema_cli migrate` verifies an unversioned database against the exact current shape, the explicit `20260917_0011` shape, or the original `20260904_0001` baseline before stamping. The `0011` path stamps that historical revision and then executes `0012`; unknown or drifted constraints refuse the stamp. The migration runs under the existing transaction and advisory lock and checks the final head shape before commit.
2. Run the worker in a dedicated subscription container with numeric UID/GID `10001:10001`, its own `HOME` and `CODEX_HOME` containing a separately authorized ChatGPT CLI login, and no Linux capabilities. Do not copy `auth.json` from another identity or expose it during provisioning. The existing host `codex-worker` UID 998 cannot traverse G2's `0700` volume: a read-only mount alone does not grant access. The container's UID 10001 matches the G2 owner; its **read-only** bind of the named managed volume at `/workspace/worktrees/managed` permits traversal of both existing and newly created `0700` directories and `0400` manifests without changing G2 modes or ACLs. Use a separate mount namespace and private writable result, ephemeral token directory and home mounts owned by UID 10001. Do not mount the writable managed volume, DB, manager Basic credential, Docker socket, production paths or provider keys. The ephemeral token directory must be writable for unlink by UID 10001; a read-only token mount cannot satisfy this protocol. Network access must reach only the CP broker and Codex service; if the broker URL is loopback, put its forwarding listener in this container's network namespace. The container image needs Python project dependencies, Git and official Codex CLI; CP image remains Git-free. Do not run the current host UID 998 command against the G2 volume without this identity arrangement.
   **STAGING provision gate:** before allowing a claim, an operator must verify inside the subscription container: `test "$(id -u)" = 10001`; `test -r /workspace/worktrees/managed/<prepared-id>/.git/ai-orchestra-workspace.json`; `test -z "$(git -C /workspace/worktrees/managed/<prepared-id> status --porcelain --untracked-files=all --ignored=matching)"`; and `python3 -c 'import os; assert os.statvfs("/workspace/worktrees/managed").f_flag & os.ST_RDONLY'`. Repeat these checks on a workspace created **after** container startup. Also verify a separate ChatGPT login with `codex login status` in this identity, the ephemeral token directory ownership and mode, absence of the Docker socket, and token pathname absence in the model context. A failed gate prevents worker start. This is staging provisioning guidance; no container, credential or live mount was provisioned by this change. Deployment on the current UID 998 VPS arrangement is **BLOCKED** until the dedicated UID 10001 setup is demonstrated.
3. Start from this repository root with the project Python dependencies and official Codex CLI installed. Verify `codex login status` says ChatGPT. Launch:

   ```bash
   python3 -m scripts.codex_execution_worker \
     --broker http://127.0.0.1:8000 \
     --token-file /run/codex-subscription-private/one-shot.token \
     --output /private/codex-results --once
   ```

   Omit `--once` to poll continuously. The broker is token-only; the worker never uses the manager Basic API. The worker verifies Linux `PR_SET_DUMPABLE=0` before token consumption, preventing a same-UID model child from ptracing its supervisor memory. It passes an allowlisted environment and closes unrelated descriptors. Keep output outside web mounts.
4. Create a development task assigned to a ready repository, then call execute with manager auth and `X-Control-Request: ai-orchestra`. Inspect `/api/executions/{id}`, `/timeline`, `/result-package`, and the private `events.jsonl`, `final.txt`, `stderr.log`, `summary.json` per role. Confirm three role artifacts and no OpenCode session. Run test DB API and worker smoke before any release.

## Lease and rollback

Claims increment `lease_generation`; heartbeat renews a 120 second lease while pending or running. A failed local G2 check or rejected acknowledgement never starts inference and leaves the pending lease to expire. Expired pending and running leases become failed and are **never reclaimed** because a worker may still be verifying or inferring. A stale generation cannot acknowledge preflight or submit a result. The worker stops its process group when a heartbeat is rejected or its run deadline elapses. Operator intervention is required for retry with a new run. No automatic model, backend or paid API fallback occurs. Manager `/abort` on an unclaimed or preflight-pending subscription run immediately fences and finalizes it as cancelled. For a running model, `/abort` fences the old lease generation, marks `cancel_requested`, and bounds the remaining lease to 30 seconds without claiming that the child process has already stopped. The worker must kill the model process group when heartbeat/result submission is rejected; the next broker claim reconciles expired cancellation and creates a terminal cancelled Result Package. If every worker is offline, `cancel_requested` remains visibly active and blocks new runs until a supervised worker resumes reconciliation; there is no unsafe automatic re-execution. Cgroup supervision remains mandatory. Monthly subscription costs and usage are marked unknown rather than reported as a measured zero.
The service supervisor must contain all Codex descendants in a cgroup because `SIGKILL` cannot be caught by Python. For systemd, use `KillMode=control-group`, `TimeoutStopSec=30s`, `SendSIGKILL=yes`, a finite `MemoryMax` and a finite `TasksMax`; test forced termination. Missing cgroup containment blocks deployment. The worker handles SIGTERM/SIGINT by killing the active process group, bounds `codex login status` to 15 seconds, and writes the prompt without blocking heartbeat checks.

Example service stop and resource policy for the dedicated container supervisor (staging template, not installed here):

```ini
[Service]
KillMode=control-group
TimeoutStopSec=30s
SendSIGKILL=yes
MemoryMax=4G
TasksMax=128
```

To switch off, stop the worker and remove `CONTROL_PLANE_CODEX_WORKER_TOKEN_FILE` from CP configuration. New subscription start requests return 503 before creating a run; existing queued runs retain their history and cannot silently switch to paid execution. Expired leases and deadlines are marked failed when a worker next calls claim. Retain artifacts and database history. Migration downgrade refuses while subscription rows exist, so rollback of the application should use a compatible build or be postponed until history is archived under an approved retention process. Do not delete production rows to force it.

## Limits of this slice

The CP-integrated path is intentionally read-only and accepts only `general-standard` repositories; higher assurance tiers require their existing provenance and runner controls. The stage-1 operator `scripts/codex_task.py` still supports an explicitly authorized developer worktree with `--role developer --allow-write`, but it does not attach that run to CP. No CP-integrated code-editing role, deploy, external write or secrets access is granted. Evidence file contents are trusted worker output and need host-level retention and audit controls. This repository does not install or start a host service, provision credentials, change production Compose mounts, or run a live migration.
