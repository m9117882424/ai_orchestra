#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

export COMPOSE_PROJECT_NAME="ai-orchestra-postgres-schema-smoke-${GITHUB_RUN_ID:-$$}"
if [[ "$COMPOSE_PROJECT_NAME" == "ai-development-department" ]]; then
  echo "[FAIL] Refusing destructive schema smoke in production Compose namespace" >&2
  exit 1
fi

SMOKE_TMP="$(mktemp -d /tmp/ai-orchestra-schema-smoke.XXXXXX)"
SMOKE_CONTROL_IMAGE="ai-orchestra/control-plane-schema-smoke:${GITHUB_RUN_ID:-$$}"
SMOKE_OVERRIDE="$SMOKE_TMP/images.yml"
printf 'services:
  control-plane:
    image: %s
' "$SMOKE_CONTROL_IMAGE" > "$SMOKE_OVERRIDE"
export COMPOSE_FILE="$project_root/docker-compose.yml:$SMOKE_OVERRIDE"

docker build --pull --target control-plane -t "$SMOKE_CONTROL_IMAGE"   -f control_plane/Dockerfile control_plane >/dev/null

cleanup() {
  docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  docker image rm -f "$SMOKE_CONTROL_IMAGE" >/dev/null 2>&1 || true
  rm -rf "$SMOKE_TMP"
}
trap cleanup EXIT

wait_postgres() {
  for _ in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U ai_orchestra -d ai_orchestra >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "[FAIL] PostgreSQL did not become ready" >&2
  docker compose logs postgres >&2 || true
  return 1
}

schema_cli() {
  docker compose run --rm -T --no-deps \
    -e CONTROL_PLANE_ENVIRONMENT=production \
    -e CONTROL_PLANE_SERVER_PASSWORD=ci-only-manager-password-000000 \
    -e CONTROL_PLANE_OPENCODE_PASSWORD=ci-only-opencode-password-000000 \
    control-plane python -m app.schema_cli "$@"
}

echo "[INFO] PostgreSQL legacy-adoption smoke"
docker compose up -d postgres >/dev/null
wait_postgres

docker compose run --rm -T --no-deps \
  -e CONTROL_PLANE_ENVIRONMENT=test \
  control-plane python - <<'PY'
from app.database_base import Base
from app.database_engine import create_configured_engine
import app.models  # noqa: F401

engine = create_configured_engine()
Base.metadata.create_all(engine)
print("[OK] Legacy unversioned schema created")
PY

schema_cli migrate
schema_cli check

echo "[INFO] PostgreSQL Repository Registry constraint smoke"
if docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c "INSERT INTO repositories (
        id, name, remote_url, remote_identity, remote_host, provider,
        enabled, status, execution_profile, assurance_tier, version
      ) VALUES (
        'invalid-registry-row', 'invalid-registry-row',
        'https://github.com/example/invalid.git',
        'github.com/example/invalid', 'github.com', 'github',
        TRUE, 'trusted_without_validation', 'development',
        'general-standard', 1
      );" >/dev/null 2>&1; then
  echo "[FAIL] PostgreSQL accepted forged Repository Registry status" >&2
  exit 1
fi

invalid_rows="$(docker compose exec -T postgres \
  psql -U ai_orchestra -d ai_orchestra -Atc \
  "SELECT count(*) FROM repositories WHERE id = 'invalid-registry-row'")"
if [[ "$invalid_rows" != "0" ]]; then
  echo "[FAIL] Rejected Repository Registry row was not rolled back" >&2
  exit 1
fi

if docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c "INSERT INTO repositories (
        id, name, remote_url, remote_identity, remote_host, provider,
        enabled, status, execution_profile, assurance_tier, version,
        sync_generation, sync_failure_count
      ) VALUES (
        'invalid-ready-row', 'invalid-ready-row',
        'https://github.com/example/invalid-ready.git',
        'github.com/example/invalid-ready', 'github.com', 'github',
        TRUE, 'ready', 'development', 'general-standard', 1, 0, 0
      );" >/dev/null 2>&1; then
  echo "[FAIL] PostgreSQL accepted ready repository without trusted sync evidence" >&2
  exit 1
fi

echo "[INFO] PostgreSQL Repo Manager lease-pair constraint smoke"
if docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c "INSERT INTO repositories (
        id, name, remote_url, remote_identity, remote_host, provider,
        enabled, status, execution_profile, assurance_tier, version,
        sync_generation, sync_failure_count, sync_lease_owner
      ) VALUES (
        'invalid-lease-row', 'invalid-lease-row',
        'https://github.com/example/invalid-lease.git',
        'github.com/example/invalid-lease', 'github.com', 'github',
        TRUE, 'validating', 'development', 'general-standard', 1,
        1, 0, 'worker-without-expiry'
      );" >/dev/null 2>&1; then
  echo "[FAIL] PostgreSQL accepted half-populated Repo Manager lease" >&2
  exit 1
fi

echo "[INFO] PostgreSQL Repo Manager validating-state constraint smoke"
if docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c "INSERT INTO repositories (
        id, name, remote_url, remote_identity, remote_host, provider,
        enabled, status, execution_profile, assurance_tier, version,
        sync_generation, sync_failure_count, sync_lease_owner,
        sync_lease_expires_at
      ) VALUES (
        'invalid-validating-row', 'invalid-validating-row',
        'https://github.com/example/invalid-validating.git',
        'github.com/example/invalid-validating', 'github.com', 'github',
        TRUE, 'validating', 'development', 'general-standard', 1,
        1, 0, 'worker', CURRENT_TIMESTAMP + INTERVAL '5 minutes'
      );" >/dev/null 2>&1; then
  echo "[FAIL] PostgreSQL accepted validating repository without start evidence" >&2
  exit 1
fi

echo "[INFO] PostgreSQL Runner Manager durable-state constraint smoke"
docker compose run --rm -T --no-deps \
  -e CONTROL_PLANE_ENVIRONMENT=test \
  control-plane python - <<'PYRUNNER'
from datetime import datetime, timezone
from app.db import SessionLocal
from app.models import ExecutionRun, Repository, RunnerJob, Task, TaskWorkspace

now = datetime.now(timezone.utc)
repo_id = "11111111-1111-4111-8111-111111111111"
task_id = "22222222-2222-4222-8222-222222222222"
workspace_id = "33333333-3333-4333-8333-333333333333"
execution_id = "44444444-4444-4444-8444-444444444444"
job_id = "55555555-5555-4555-8555-555555555555"
commit = "a" * 40
tree = "b" * 40
digest = "c" * 64
with SessionLocal() as db:
    db.add(Repository(
        id=repo_id, name="runner-smoke", remote_url="https://github.com/example/runner-smoke.git",
        remote_identity="github.com/example/runner-smoke", remote_host="github.com",
        provider="github", enabled=True, status="ready", default_branch="main",
        last_known_commit=commit, last_fetched_at=now, sync_finished_at=now,
        sync_next_at=now,
    ))
    db.add(Task(id=task_id, title="runner smoke", repository_id=repo_id, status="in_progress"))
    db.add(TaskWorkspace(
        id=workspace_id, task_id=task_id, repository_id=repo_id, status="ready",
        base_commit=commit, base_branch="main",
        branch_name="ai-orchestra/task-222222222222/run-44444444444444448444444444444444",
        opencode_path=f"/workspace/worktrees/managed/{workspace_id}", initial_tree=tree,
        preflight_digest=digest, tracked_entries=1, prepared_at=now, version=1,
    ))
    db.flush()
    db.add(ExecutionRun(
        id=execution_id, task_id=task_id, contract_version=2, repository_id=repo_id,
        workspace_id=workspace_id, base_commit=commit,
        workspace_path=f"/workspace/worktrees/managed/{workspace_id}", workspace_tree=tree,
        workspace_preflight_digest=digest, workspace_preflight_completed_at=now,
        workspace_runtime_preflight_digest=digest, workspace_runtime_verified_at=now,
        status="running", stage="department_lead", opencode_session_id="runner-smoke-session",
    ))
    db.flush()
    db.add(RunnerJob(
        id=job_id, execution_id=execution_id, repository_id=repo_id, workspace_id=workspace_id,
        idempotency_key="66666666-6666-4666-8666-666666666666", status="queued",
        argv=["true"], timeout_seconds=30, base_commit=commit, preflight_digest=digest,
        stdout="", stderr="", output_truncated=False, cleanup_confirmed=None,
        next_attempt_at=now, created_at=now, updated_at=now,
    ))
    db.commit()
print("[OK] durable runner fixture seeded")
PYRUNNER

if docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c "UPDATE runner_jobs SET status='completed', next_attempt_at=NULL,
      finished_at=CURRENT_TIMESTAMP WHERE id='55555555-5555-4555-8555-555555555555';" \
  >/dev/null 2>&1; then
  echo "[FAIL] PostgreSQL accepted completed runner job without cleanup/image evidence" >&2
  exit 1
fi

if docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c "UPDATE runner_jobs SET status='running', next_attempt_at=NULL,
      lease_owner='half-lease', lease_expires_at=NULL
      WHERE id='55555555-5555-4555-8555-555555555555';" >/dev/null 2>&1; then
  echo "[FAIL] PostgreSQL accepted half-populated runner lease" >&2
  exit 1
fi

if docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c "UPDATE runner_jobs SET next_attempt_at=NULL
      WHERE id='55555555-5555-4555-8555-555555555555';" >/dev/null 2>&1; then
  echo "[FAIL] PostgreSQL accepted queued runner job without next_attempt_at" >&2
  exit 1
fi

if docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c "UPDATE runner_jobs SET source_snapshot_digest='bad'
      WHERE id='55555555-5555-4555-8555-555555555555';" >/dev/null 2>&1; then
  echo "[FAIL] PostgreSQL accepted invalid runner source snapshot digest" >&2
  exit 1
fi

if docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c "UPDATE runner_jobs SET checkpoint_digest='dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd'
      WHERE id='55555555-5555-4555-8555-555555555555';" >/dev/null 2>&1; then
  echo "[FAIL] PostgreSQL accepted partial runner checkpoint binding" >&2
  exit 1
fi

echo "[OK] PostgreSQL Runner Manager constraints reject forged state"

echo "[INFO] PostgreSQL drift refusal smoke"
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c 'DROP INDEX ix_execution_runs_task_id;' >/dev/null
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c 'ALTER TABLE repositories DROP CONSTRAINT ck_repositories_status;' >/dev/null
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c 'ALTER TABLE repositories DROP CONSTRAINT ck_repositories_sync_lease_pair;' >/dev/null
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c 'ALTER TABLE repositories DROP CONSTRAINT ck_repositories_validating_state;' >/dev/null
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c 'ALTER TABLE runner_jobs DROP CONSTRAINT ck_runner_jobs_cleanup_state;' >/dev/null
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c 'ALTER TABLE runner_jobs DROP CONSTRAINT ck_runner_jobs_checkpoint_binding;' >/dev/null
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c 'DROP INDEX ux_runner_jobs_checkpoint_command;' >/dev/null

if schema_cli check; then
  echo "[FAIL] schema check accepted deliberately removed index/constraint" >&2
  exit 1
fi

echo "[INFO] PostgreSQL fresh-migration smoke"
docker compose down -v --remove-orphans >/dev/null
docker compose up -d postgres >/dev/null
wait_postgres

schema_cli migrate
schema_cli check

echo "[OK] PostgreSQL schema smoke passed"
