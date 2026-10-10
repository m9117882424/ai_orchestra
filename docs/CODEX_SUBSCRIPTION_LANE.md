# Codex subscription lane — stage 2 runbook

## Supported contract

`POST /api/tasks/{task_id}/execute` defaults to `{"backend":"codex-subscription"}` for development tasks. The request also accepts `{"backend":"opencode"}` only when the server has `CONTROL_PLANE_ALLOW_PAID_OPENCODE_EXECUTION=true`; without both conditions it returns 403. Unknown backends return 422. There is no fallback between providers. Existing rows migrate to `backend=opencode` and keep their existing owner. Old failed runs are not replayed.

The Codex path preserves repository readiness, manager Basic + mutation header, task lock, workspace-manager G2 preflight, immutable base commit/tree, and the existing approval and CapabilityGuard boundaries. `preparing` becomes `queued` only after G2 prepares the managed workspace. The broker claims only queued subscription rows. The OpenCode worker claims only OpenCode rows.

Worker API uses `Authorization: Bearer <private token>` and only `POST /api/codex-worker/claim`, `POST /api/codex-worker/{run_id}/heartbeat` with `generation`, and `POST /api/codex-worker/{run_id}/phase` with `generation`, `role`, `outcome`, `model`, `final_text`, `final_sha256`, `events_sha256`, `artifact_path`, `base_commit`, `base_tree`, and `workspace_preflight_digest`. It exposes no arbitrary command field. `role` is architect, qa or reviewer in that order; outcome is completed, execution_failed, local_timeout, incomplete_result, read_only_violation, auth_failed or workspace_failed.

The worker performs `architect → qa → reviewer`, each as a separate read-only Codex invocation and separate `execution_child_runs`/`execution_evidence` row. The CP verifies the G2 database binding and repository trust at claim and result acceptance. The trusted worker verifies the G2 manifest, commit, tree, branch and clean status before and after each role, and submits the preflight digest. A role requires process success, `turn.completed`, nonempty final report and unchanged workspace. On failure the run fails; the task is never marked done. Successful review leaves the task in `qa` for the existing human process. Terminal runs request G2 inspection, so the Result Package is provisional until that inspection completes. Subscription usage and cost are `unknown`, including the G4 cost summary, not zero rubles. Artifacts are private files on the worker host; the CP stores their path, checksums, bounded final report and immutable base identifiers. CP cannot independently read its private events file or repeat the filesystem check. Preserve worker evidence directories for inspection.

## Isolated staging setup

1. Apply additive Alembic revision `20261010_0012` to a **test** database only. Deploying to production requires a separate release decision. Generate a random 32+ character token outside the repository and store it as a regular file mode `0600`, readable by CP and `codex-worker` via separate read-only mount or controlled copy. Set `CONTROL_PLANE_CODEX_WORKER_TOKEN_FILE` to the CP-local path. Do not put the token in `.env`, Git, DB, prompt or logs.
2. Run the worker as an unprivileged dedicated `codex-worker` account with its own `HOME` and `CODEX_HOME` containing ChatGPT CLI login. No API key, DB credential, manager Basic credential, Docker socket, production mount or `/var/lib/corporate_taxi` is allowed. Mount **only** the G2 managed workspace root read-only at its canonical `/workspace/worktrees/managed` path in a separate mount namespace. G2 owns the volume as UID 10001 with mode 0700: provision the worker's namespace with a reviewed UID mapping that can read this volume. Do not relax G2 permissions or share the raw writable volume. The account must have access to an isolated private result directory. Do not give it privileged groups. Network access should be limited to loopback CP broker and the Codex service; no arbitrary egress or external writes.
3. Start from this repository root with the project Python dependencies and official Codex CLI installed. Verify `codex login status` says ChatGPT. Launch:

   ```bash
   python3 -m scripts.codex_execution_worker \
     --broker http://127.0.0.1:8000 \
     --token-file /private/codex-worker.token \
     --output /private/codex-results --once
   ```

   Omit `--once` to poll continuously. The broker is token-only; the worker never uses the manager Basic API. Keep the token file and output directory private and outside web or container mounts.
4. Create a development task assigned to a ready repository, then call execute with manager auth and `X-Control-Request: ai-orchestra`. Inspect `/api/executions/{id}`, `/timeline`, `/result-package`, and the private `events.jsonl`, `final.txt`, `stderr.log`, `summary.json` per role. Confirm three role artifacts and no OpenCode session. Run test DB API and worker smoke before any release.

## Lease and rollback

Claims increment `lease_generation`; heartbeat renews a 120 second lease. Expired running leases become failed and are **never reclaimed** because an old inference process may still exist. A stale generation cannot submit a result. The worker stops its process group when a heartbeat is rejected or its run deadline elapses. Operator intervention is required for retry with a new run. No automatic model or backend retry occurs.

To switch off, stop the worker and remove `CONTROL_PLANE_CODEX_WORKER_TOKEN_FILE` from CP configuration. New subscription tasks remain queued; they cannot silently switch to paid execution. Expired leases and deadlines are marked failed when a worker next calls claim. Retain artifacts and database history. Migration downgrade refuses while subscription rows exist, so rollback of the application should use a compatible build or be postponed until history is archived under an approved retention process. Do not delete production rows to force it.

## Limits of this slice

The CP-integrated path is intentionally read-only and accepts only `general-standard` repositories; higher assurance tiers require their existing provenance and runner controls. The stage-1 operator `scripts/codex_task.py` still supports an explicitly authorized developer worktree with `--role developer --allow-write`, but it does not attach that run to CP. No CP-integrated code-editing role, deploy, external write or secrets access is granted. Evidence file contents are trusted worker output and need host-level retention and audit controls. This repository does not install or start a host service, provision credentials, change production Compose mounts, or run a live migration.
