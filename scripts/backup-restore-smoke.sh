#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-ai-orchestra-backup-restore-smoke-${GITHUB_RUN_ID:-$$}}"
if [[ ! "$COMPOSE_PROJECT_NAME" =~ ^ai-orchestra-backup-restore-smoke-[A-Za-z0-9_.-]+$ ]]; then
  echo "[FAIL] Backup/restore smoke requires an isolated ai-orchestra-backup-restore-smoke-* namespace" >&2
  exit 1
fi
BACKUP_ROOT="${BACKUP_ROOT:-$(mktemp -d /tmp/ai-orchestra-backup-smoke.XXXXXX)}"
if [[ "$BACKUP_ROOT" != /tmp/ai-orchestra-backup-smoke.* ]]; then
  echo "[FAIL] Backup/restore smoke BACKUP_ROOT must be an isolated /tmp path" >&2
  exit 1
fi
mkdir -p "$BACKUP_ROOT"
export BACKUP_ROOT
SMOKE_TMP="$(mktemp -d /tmp/ai-orchestra-backup-runtime.XXXXXX)"
SMOKE_CONTROL_IMAGE="ai-orchestra/control-plane-backup-smoke:${GITHUB_RUN_ID:-$$}"
SMOKE_WORKSPACE_IMAGE="ai-orchestra/workspace-manager-backup-smoke:${GITHUB_RUN_ID:-$$}"
SMOKE_OVERRIDE="$SMOKE_TMP/images.yml"
printf 'services:
  control-plane:
    image: %s
  workspace-manager:
    image: %s
  workspace-volume-init:
    image: %s
'   "$SMOKE_CONTROL_IMAGE" "$SMOKE_WORKSPACE_IMAGE" "$SMOKE_WORKSPACE_IMAGE" > "$SMOKE_OVERRIDE"
export COMPOSE_FILE="$project_root/docker-compose.yml:$SMOKE_OVERRIDE"
baseline_sql="$(mktemp /tmp/ai-orchestra-baseline-0001.XXXXXX.sql)"

docker build --pull --target control-plane -t "$SMOKE_CONTROL_IMAGE"   -f control_plane/Dockerfile control_plane >/dev/null
docker build --pull --target workspace-manager -t "$SMOKE_WORKSPACE_IMAGE"   -f control_plane/Dockerfile control_plane >/dev/null

cleanup() {
  docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$BACKUP_ROOT"
  rm -f "$baseline_sql"
  docker image rm -f "$SMOKE_CONTROL_IMAGE" "$SMOKE_WORKSPACE_IMAGE" >/dev/null 2>&1 || true
  rm -rf "$SMOKE_TMP"
}
trap cleanup EXIT

wait_postgres() {
  local container_id
  container_id="$(docker compose ps -q postgres)"
  if [[ -z "$container_id" ]]; then
    echo "[FAIL] PostgreSQL container was not created" >&2
    return 1
  fi

  local init_complete=0
  for _ in $(seq 1 60); do
    if [[ "$(docker inspect "$container_id" --format '{{.State.Running}}' 2>/dev/null || true)" != "true" ]]; then
      echo "[FAIL] PostgreSQL stopped during CI initialization" >&2
      docker compose logs postgres >&2 || true
      return 1
    fi
    if docker compose logs --no-color postgres 2>&1 | grep -Fq 'PostgreSQL init process complete; ready for start up.'; then
      init_complete=1
      break
    fi
    sleep 1
  done
  if [[ "$init_complete" != "1" ]]; then
    echo "[FAIL] PostgreSQL CI initialization did not complete" >&2
    docker compose logs postgres >&2 || true
    return 1
  fi

  for _ in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U ai_orchestra -d ai_orchestra >/dev/null 2>&1 \
      && [[ "$(docker compose exec -T postgres psql -U ai_orchestra -d ai_orchestra -Atc 'SELECT 1' 2>/dev/null || true)" == "1" ]]; then
      return 0
    fi
    sleep 1
  done
  echo "[FAIL] PostgreSQL final CI database did not become ready" >&2
  docker compose logs postgres >&2 || true
  return 1
}

echo "[INFO] Creating exact historical 0001 PostgreSQL source database"
docker compose up -d postgres >/dev/null
wait_postgres

# Generate the historical revision through Alembic itself, but in offline SQL mode.
# Applying that SQL with psql inside the PostgreSQL container keeps this fixture
# independent of cross-container password authentication while still proving that
# the source schema is exactly what revision 0001 declares.
docker compose run --rm -T --no-deps \
  -e CONTROL_PLANE_ENVIRONMENT=test \
  -e CONTROL_PLANE_DATABASE_URL=postgresql+psycopg://ai_orchestra:offline-only@postgres:5432/ai_orchestra \
  control-plane python -m alembic -c alembic.ini upgrade 20260904_0001 --sql \
  > "$baseline_sql"

if [[ ! -s "$baseline_sql" ]]; then
  echo "[FAIL] Alembic did not emit SQL for historical revision 20260904_0001" >&2
  exit 1
fi

docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  < "$baseline_sql" >/dev/null

source_revision="$(docker compose exec -T postgres psql -U ai_orchestra -d ai_orchestra -Atc \
  'SELECT version_num FROM alembic_version LIMIT 1')"
if [[ "$source_revision" != "20260904_0001" ]]; then
  echo "[FAIL] Expected historical source revision 20260904_0001, got: $source_revision" >&2
  exit 1
fi

# Production before Alembic adoption could contain the baseline shape without a
# marker. Reproduce that exact state so restore exercises fail-closed recognition:
# verify 0001 shape -> stamp 0001 -> upgrade through lease 0002, dispatch 0003,
# deadline/cancellation 0004, Repository Registry 0005, and trusted Repo Manager
# durable synchronization state 0006, task workspaces 0007, and durable runner jobs 0008.
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c 'DROP TABLE alembic_version' >/dev/null

if docker compose exec -T postgres psql -U ai_orchestra -d ai_orchestra -Atc \
  "SELECT to_regclass('public.alembic_version') IS NOT NULL" | grep -qx t; then
  echo "[FAIL] CI source was expected to be unversioned after baseline marker removal" >&2
  exit 1
fi

docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra <<'SQL' >/dev/null
INSERT INTO audit_events (id, actor, action, entity_type, entity_id, details, created_at)
VALUES ('dr-smoke-marker', 'ci', 'backup_restore_smoke', 'test', 'dr-smoke-marker', '{}'::jsonb, NOW());
SQL

echo "[INFO] Creating and verifying historical CI backup"
bash ./scripts/backup.sh
archive="$(find "$BACKUP_ROOT" -maxdepth 1 -type f -name 'ai-orchestra-*.tar.gz' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"
bash ./scripts/verify-backup.sh "$archive"

echo "[INFO] Executing clean restore + historical adoption drill"
bash ./scripts/restore-drill.sh "$archive"
evidence="$(find "$BACKUP_ROOT/drills" -maxdepth 1 -type f -name 'restore-drill-*.json' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"

python3 - "$archive" "$evidence" <<'PY'
import hashlib
import json
import pathlib
import sys

archive = pathlib.Path(sys.argv[1]).resolve()
evidence = pathlib.Path(sys.argv[2])
payload = json.loads(evidence.read_text(encoding="utf-8"))
sha = hashlib.sha256(archive.read_bytes()).hexdigest()
assert payload["result"] == "success"
assert payload["source_backup_sha256"] == sha
assert payload["pre_migration_revision"] == "unversioned"
assert payload["post_migration_revision"] == "20260917_0011"
assert payload["restored_table_counts"].get("audit_events", 0) >= 1
assert payload["restored_table_counts"].get("alembic_version", 0) == 1
assert payload["restored_table_counts"].get("repositories") == 0
assert payload["restored_table_counts"].get("task_workspaces") == 0
assert payload["restored_table_counts"].get("runner_jobs") == 0
assert payload["task_workspace_restore"] == {
    "backup_format": 3,
    "cleaning_rows_verified": 0,
    "database_rows": 0,
    "required_workspace_directories_verified": 0,
    "workspace_manifests_verified": 0,
}
assert payload["runner_job_restore"] == {"bindings_verified": 0, "checkpoint_bindings_verified": 0, "database_rows": 0}
assert payload["observed_restore_rto_seconds"] >= 0
assert payload["observed_backup_age_seconds"] >= 0
print("[OK] Historical 0001 backup was restored, adopted to 0011 and retained the seeded audit marker")
PY

echo "[INFO] Creating current-head backup with durable runner-job evidence"
docker compose down -v --remove-orphans >/dev/null
rm -rf "$BACKUP_ROOT"
docker compose up -d postgres >/dev/null
wait_postgres

docker compose run --rm -T --no-deps \
  -e CONTROL_PLANE_ENVIRONMENT=test \
  control-plane python -m app.schema_cli migrate

docker compose run --rm -T --no-deps \
  -e CONTROL_PLANE_ENVIRONMENT=test \
  control-plane python - <<'PYRUNNER'
from datetime import datetime, timezone
from uuid import uuid4
from app.db import SessionLocal
from app.evidence import materialize_result_package, record_evidence
from app.models import ExecutionChildRun, ExecutionRun, Repository, RunnerJob, Task, TaskWorkspace, UsageEvent

now = datetime.now(timezone.utc)
repo_id, task_id, workspace_id, run_id, job_id, child_id = [str(uuid4()) for _ in range(6)]
idempotency_key = str(uuid4())
commit = "a" * 40
tree = "b" * 40
digest = "c" * 64
snapshot_digest = "e" * 64
checkpoint_digest = "f" * 64
image_id = "sha256:" + "d" * 64
with SessionLocal() as db:
    db.add(Repository(
        id=repo_id, name="dr-runner-repo", remote_url="https://github.com/example/dr-runner.git",
        remote_identity="github.com/example/dr-runner", remote_host="github.com", provider="github",
        enabled=True, status="ready", default_branch="main", last_known_commit=commit,
        last_fetched_at=now, sync_failure_count=0, sync_finished_at=now, sync_next_at=now,
    ))
    db.add(Task(id=task_id, title="DR runner evidence", repository_id=repo_id, status="qa"))
    db.add(TaskWorkspace(
        id=workspace_id, task_id=task_id, repository_id=repo_id, status="retained",
        base_commit=commit, base_branch="main",
        branch_name=f"ai-orchestra/task-{task_id.replace('-', '')[:12]}/run-{run_id.replace('-', '')}",
        opencode_path=f"/workspace/worktrees/managed/{workspace_id}", initial_tree=tree,
        preflight_digest=digest, tracked_entries=1, current_head_commit=commit, current_tree=tree,
        change_digest="1" * 64, has_changes=False, changed_file_count=0, inspected_at=now, version=1,
    ))
    db.flush()
    db.add(ExecutionRun(
        id=run_id, task_id=task_id, contract_version=2, repository_id=repo_id,
        workspace_id=workspace_id, base_commit=commit,
        workspace_path=f"/workspace/worktrees/managed/{workspace_id}", workspace_tree=tree,
        workspace_preflight_digest=digest, workspace_preflight_completed_at=now,
        workspace_runtime_preflight_digest=digest, workspace_runtime_verified_at=now,
        status="completed", stage="manager_review", finished_at=now,
    ))
    db.flush()
    db.add(ExecutionChildRun(
        id=child_id, execution_id=run_id, source="opencode", source_run_id="ses-dr-qa",
        parent_source_run_id="ses-dr-root", parent_call_id="call-dr-qa",
        role="qa-engineer", provider="ci", model="orchestra-qa", status="completed",
        task_fingerprint="9" * 64, attempt=1, started_at=now, finished_at=now,
        last_observed_at=now, created_at=now, updated_at=now,
    ))
    db.flush()
    db.add(RunnerJob(
        id=job_id, execution_id=run_id, repository_id=repo_id, workspace_id=workspace_id,
        idempotency_key=idempotency_key, status="completed",
        argv=["python3", "-c", "print('ok')"], timeout_seconds=30,
        base_commit=commit, preflight_digest=digest, source_snapshot_digest=snapshot_digest,
        checkpoint_digest=checkpoint_digest, checkpoint_command_index=0, checkpoint_label="dr-checkpoint",
        runner_image_id=image_id, exit_code=0, stdout="ok\n", stderr="", output_truncated=False,
        cleanup_confirmed=True, lease_generation=1, failure_count=0,
        started_at=now, finished_at=now,
    ))
    db.add(UsageEvent(
        task_id=task_id, execution_id=run_id, source="opencode-session",
        source_key="ses-dr-qa", child_run_id=child_id, role="qa-engineer", provider="ci",
        model="orchestra-qa", input_tokens=10, output_tokens=5, cost=0,
    ))
    record_evidence(
        db, execution_id=run_id, source="ci", source_key="dr-tool", kind="tool",
        role="qa", model="orchestra-qa", tool_name="pytest", status="completed",
        details={"fixture": "backup-restore-smoke"}, occurred_at=now,
    )
    materialize_result_package(db, run_id, final=True, now=now)
    workspace = db.get(TaskWorkspace, workspace_id)
    workspace.status = "removed"
    workspace.cleaned_at = now
    db.commit()
print(job_id)
PYRUNNER

bash ./scripts/backup.sh
archive="$(find "$BACKUP_ROOT" -maxdepth 1 -type f -name 'ai-orchestra-*.tar.gz' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"
bash ./scripts/restore-drill.sh "$archive"
evidence="$(find "$BACKUP_ROOT/drills" -maxdepth 1 -type f -name 'restore-drill-*.json' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"
python3 - "$evidence" <<'PYCHECK'
import json, pathlib, sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["pre_migration_revision"] == "20260917_0011"
assert payload["post_migration_revision"] == "20260917_0011"
assert payload["restored_table_counts"].get("runner_jobs") == 1
assert payload["runner_job_restore"] == {"bindings_verified": 1, "checkpoint_bindings_verified": 1, "database_rows": 1}
assert payload["restored_table_counts"].get("task_workspaces") == 1
assert payload["restored_table_counts"].get("execution_evidence") == 1
assert payload["restored_table_counts"].get("execution_child_runs") == 1
assert payload["restored_table_counts"].get("execution_result_packages") == 1
assert payload["restored_table_counts"].get("usage_events") == 1
print("[OK] Current-head runner/evidence/result-package data survived backup/restore")
PYCHECK

echo "[OK] Backup/restore smoke passed"
