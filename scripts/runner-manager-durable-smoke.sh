#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SUFFIX="${GITHUB_RUN_ID:-$$}"
PROJECT="ai-orchestra-g3-runner-smoke-$SUFFIX"
TAG="ai-orchestra/runner-manager-smoke:$SUFFIX"
CONTROL_TAG="ai-orchestra/control-plane-runner-smoke:$SUFFIX"
MANAGER_TAG="ai-orchestra/runner-manager-control-smoke:$SUFFIX"
VOLUME="ai-orchestra-runner-manager-smoke-$SUFFIX"
TMP_DIR="$(mktemp -d)"
SOCKET="$TMP_DIR/runnerd.sock"
LOG="$TMP_DIR/runnerd.log"
RUNNER_PID=""
BASE_OVERRIDE="$TMP_DIR/base-images.yml"
FULL_OVERRIDE="$TMP_DIR/full-images.yml"
printf 'services:
  control-plane:
    image: %s
' "$CONTROL_TAG" > "$BASE_OVERRIDE"
printf 'services:
  control-plane:
    image: %s
  runner-manager:
    image: %s
'   "$CONTROL_TAG" "$MANAGER_TAG" > "$FULL_OVERRIDE"
BASE=(docker compose -p "$PROJECT" -f docker-compose.yml -f "$BASE_OVERRIDE")
FULL=(docker compose -p "$PROJECT" -f docker-compose.yml -f deploy/docker-compose.runner-manager.yml -f "$FULL_OVERRIDE")

uuid() { python3 -c 'import uuid; print(uuid.uuid4())'; }
JOB1="$(uuid)"
JOB2="$(uuid)"
JOB3="$(uuid)"
IDEM1="$(uuid)"
IDEM2="$(uuid)"
IDEM3="$(uuid)"
REPO_ID="$(uuid)"
TASK_ID="$(uuid)"
WS_ID="$(uuid)"
EX_ID="$(uuid)"
COMMIT=""
DIGEST="$(python3 -c 'print("d"*64)')"

cleanup() {
  set +e
  "${FULL[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  if [[ -n "$RUNNER_PID" ]]; then
    kill "$RUNNER_PID" 2>/dev/null || true
    wait "$RUNNER_PID" 2>/dev/null || true
  fi
  for job_id in "$JOB1" "$JOB2" "$JOB3"; do
    docker rm -f "ai-orchestra-runner-${job_id//-/}" >/dev/null 2>&1 || true
  done
  docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true
  docker image rm -f "$TAG" "$CONTROL_TAG" "$MANAGER_TAG" >/dev/null 2>&1 || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

wait_postgres() {
  for _ in $(seq 1 40); do
    if "${BASE[@]}" exec -T postgres pg_isready -U ai_orchestra -d ai_orchestra >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  "${BASE[@]}" logs postgres >&2 || true
  return 1
}

job_value() {
  local job_id="$1" column="$2"
  "${BASE[@]}" exec -T postgres psql -U ai_orchestra -d ai_orchestra -Atc \
    "SELECT $column FROM runner_jobs WHERE id='$job_id'"
}

wait_job_status() {
  local job_id="$1" expected="$2" attempts="${3:-80}"
  local current=""
  for _ in $(seq 1 "$attempts"); do
    current="$(job_value "$job_id" status 2>/dev/null || true)"
    [[ "$current" == "$expected" ]] && return 0
    sleep 0.5
  done
  echo "[FAIL] job $job_id status=$current expected=$expected" >&2
  "${FULL[@]}" logs runner-manager >&2 || true
  cat "$LOG" >&2 || true
  return 1
}

insert_job() {
  local job_id="$1" idem="$2" argv_json="$3" timeout="$4"
  "${BASE[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra -c \
    "INSERT INTO runner_jobs (
      id, execution_id, repository_id, workspace_id, idempotency_key, status,
      argv, timeout_seconds, base_commit, preflight_digest, stdout, stderr,
      output_truncated, cleanup_confirmed, lease_generation, failure_count,
      next_attempt_at, created_at, updated_at
    ) VALUES (
      '$job_id', '$EX_ID', '$REPO_ID', '$WS_ID', '$idem', 'queued',
      '$argv_json'::json, $timeout, '$COMMIT', '$DIGEST', '', '',
      FALSE, NULL, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
    );" >/dev/null
}

echo "[1/9] build disposable runner and isolated control images"
docker build --pull -t "$TAG" -f runner/Dockerfile runner >/dev/null
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$TAG")"
[[ "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
docker build --pull --target control-plane -t "$CONTROL_TAG" \
  -f control_plane/Dockerfile control_plane >/dev/null

echo "[2/9] create authoritative workspace and host runnerd"
docker volume create "$VOLUME" >/dev/null
export WS_ID
docker run --rm --user 0:0 --entrypoint sh \
  -v "$VOLUME:/v" -e WS_ID "$IMAGE_ID" -lc '
set -eu
root="/v/$WS_ID"
mkdir -p "$root"
git -C "$root" init -q
git -C "$root" config user.email smoke@example.invalid
git -C "$root" config user.name smoke
printf "authoritative\n" > "$root/marker"
git -C "$root" add marker
GIT_AUTHOR_DATE=2026-09-16T00:00:00Z GIT_COMMITTER_DATE=2026-09-16T00:00:00Z \
  git -C "$root" commit -qm initial
chown -R 10001:10001 "$root"
chmod 700 "$root" "$root/.git"
'
COMMIT="$(docker run --rm --entrypoint git -v "$VOLUME:/v:ro" -e WS_ID "$IMAGE_ID" -C "/v/$WS_ID" rev-parse HEAD)"
export WS_ID EX_ID COMMIT DIGEST
docker run --rm --user 0:0 --entrypoint python3 \
  -v "$VOLUME:/v" -e WS_ID -e EX_ID -e COMMIT -e DIGEST \
  "$IMAGE_ID" -c '
import json, os
from pathlib import Path
root = Path("/v") / os.environ["WS_ID"]
manifest = {
    "contract_version": 1,
    "workspace_id": os.environ["WS_ID"],
    "execution_id": os.environ["EX_ID"],
    "base_commit": os.environ["COMMIT"],
    "preflight_digest": os.environ["DIGEST"],
    "workspace_path": f"/workspace/worktrees/managed/{os.environ['"'"'WS_ID'"'"']}",
}
path = root / ".git" / "ai-orchestra-workspace.json"
path.write_text(json.dumps(manifest))
os.chown(path, 10001, 10001)
'
export RUNNERD_SOCKET_PATH="$SOCKET"
export RUNNERD_WORKSPACE_VOLUME="$VOLUME"
export RUNNERD_IMAGE_ID="$IMAGE_ID"
export RUNNERD_MAX_TIMEOUT_SECONDS=30
export RUNNERD_MAX_CONCURRENT=1
RUNNERD_ALLOW_NONROOT=1 python3 runner/runnerd.py serve >"$LOG" 2>&1 &
RUNNER_PID=$!
for _ in $(seq 1 50); do
  [[ -S "$SOCKET" ]] && break
  kill -0 "$RUNNER_PID" 2>/dev/null || { cat "$LOG" >&2; exit 1; }
  sleep 0.1
done
[[ -S "$SOCKET" ]]

export RUNNERD_SOCKET_HOST_PATH="$SOCKET"
export RUNNERD_SOCKET_GID
RUNNERD_SOCKET_GID="$(stat -c %g "$SOCKET")"
docker build --pull --target runner-manager -t "$MANAGER_TAG" \
  -f control_plane/Dockerfile control_plane >/dev/null

echo "[3/9] migrate isolated PostgreSQL and seed immutable binding"
"${BASE[@]}" up -d postgres >/dev/null
wait_postgres
"${BASE[@]}" run --rm -T --no-deps \
  -e CONTROL_PLANE_ENVIRONMENT=production \
  -e CONTROL_PLANE_SERVER_PASSWORD=smoke-manager-password-000000 \
  -e CONTROL_PLANE_OPENCODE_PASSWORD=smoke-opencode-password-000000 \
  control-plane python -m app.schema_cli migrate >/dev/null

export REPO_ID TASK_ID WS_ID EX_ID COMMIT DIGEST
"${BASE[@]}" run --rm -T --no-deps \
  -e CONTROL_PLANE_ENVIRONMENT=test \
  -e REPO_ID -e TASK_ID -e WS_ID -e EX_ID -e COMMIT -e DIGEST \
  control-plane python - <<'PY'
import os
from datetime import datetime, timezone
from app.db import SessionLocal
from app.models import ExecutionRun, Repository, Task, TaskWorkspace
now = datetime.now(timezone.utc)
with SessionLocal() as db:
    db.add(Repository(
        id=os.environ["REPO_ID"], name="runner-manager-smoke",
        remote_url="https://github.com/example/runner-manager-smoke.git",
        remote_identity="github.com/example/runner-manager-smoke", remote_host="github.com",
        provider="github", enabled=True, status="ready", default_branch="main",
        last_known_commit=os.environ["COMMIT"], last_fetched_at=now,
        sync_finished_at=now, sync_next_at=now,
    ))
    db.add(Task(
        id=os.environ["TASK_ID"], title="Runner Manager smoke",
        repository_id=os.environ["REPO_ID"], status="in_progress",
    ))
    db.add(TaskWorkspace(
        id=os.environ["WS_ID"], task_id=os.environ["TASK_ID"],
        repository_id=os.environ["REPO_ID"], status="ready",
        base_commit=os.environ["COMMIT"], base_branch="main",
        branch_name=f"ai-orchestra/task-{os.environ['TASK_ID'].replace('-', '')[:12]}/run-{os.environ['EX_ID'].replace('-', '')}",
        opencode_path=f"/workspace/worktrees/managed/{os.environ['WS_ID']}",
        initial_tree="b" * 40, preflight_digest=os.environ["DIGEST"], tracked_entries=2,
        prepared_at=now, version=1,
    ))
    db.flush()
    db.add(ExecutionRun(
        id=os.environ["EX_ID"], task_id=os.environ["TASK_ID"], contract_version=2,
        repository_id=os.environ["REPO_ID"], workspace_id=os.environ["WS_ID"],
        base_commit=os.environ["COMMIT"],
        workspace_path=f"/workspace/worktrees/managed/{os.environ['WS_ID']}",
        workspace_tree="b" * 40, workspace_preflight_digest=os.environ["DIGEST"],
        workspace_preflight_completed_at=now,
        workspace_runtime_preflight_digest=os.environ["DIGEST"],
        workspace_runtime_verified_at=now, status="running", stage="department_lead",
        opencode_session_id=f"smoke-{os.environ['EX_ID']}",
    ))
    db.commit()
PY
echo "[4/9] durable job reaches real runnerd and commits terminal evidence"
insert_job "$JOB1" "$IDEM1" '["sh","-c","printf durable-ok; printf disposable > runner-manager-output.txt"]' 20
"${FULL[@]}" up -d runner-manager >/dev/null
wait_job_status "$JOB1" completed 80
[[ "$(job_value "$JOB1" cleanup_confirmed)" == "t" ]]
[[ "$(job_value "$JOB1" lease_generation)" == "1" ]]
[[ "$(job_value "$JOB1" stdout)" == "durable-ok" ]]
echo "[OK] durable job completed with cleanup evidence"

echo "[5/9] simulate Runner Manager crash after sandbox start"
insert_job "$JOB2" "$IDEM2" '["sh","-c","sleep 8; printf recovery-ok"]' 20
container2="ai-orchestra-runner-${JOB2//-/}"
started=0
for _ in $(seq 1 80); do
  if docker container inspect "$container2" >/dev/null 2>&1; then
    started=1
    break
  fi
  sleep 0.1
done
[[ "$started" == "1" ]]
[[ "$(job_value "$JOB2" status)" == "running" ]]
"${FULL[@]}" stop -t 1 runner-manager >/dev/null
for _ in $(seq 1 120); do
  if ! docker container inspect "$container2" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done
if docker container inspect "$container2" >/dev/null 2>&1; then
  echo "[FAIL] runner container survived broker cleanup after manager crash" >&2
  exit 1
fi
[[ "$(job_value "$JOB2" status)" == "running" ]]
echo "[OK] ambiguous outcome remained fenced running"

echo "[6/9] expire lease and recover with a new generation"
"${BASE[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra -c \
  "UPDATE runner_jobs SET lease_expires_at=CURRENT_TIMESTAMP - INTERVAL '1 second'
   WHERE id='$JOB2' AND status='running';" >/dev/null
"${FULL[@]}" up -d runner-manager >/dev/null
wait_job_status "$JOB2" completed 100
[[ "$(job_value "$JOB2" cleanup_confirmed)" == "t" ]]
[[ "$(job_value "$JOB2" stdout)" == "recovery-ok" ]]
generation2="$(job_value "$JOB2" lease_generation)"
[[ "$generation2" -ge 2 ]]
echo "[OK] stale job recovered at generation=$generation2"

echo "[7/9] trust revocation after enqueue fails closed before runnerd"
"${BASE[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra -c \
  "BEGIN;
   INSERT INTO runner_jobs (
     id, execution_id, repository_id, workspace_id, idempotency_key, status,
     argv, timeout_seconds, base_commit, preflight_digest, stdout, stderr,
     output_truncated, cleanup_confirmed, lease_generation, failure_count,
     next_attempt_at, created_at, updated_at
   ) VALUES (
     '$JOB3', '$EX_ID', '$REPO_ID', '$WS_ID', '$IDEM3', 'queued',
     '[\"sh\",\"-c\",\"printf SHOULD_NOT_RUN\"]'::json, 20,
     '$COMMIT', '$DIGEST', '', '', FALSE, NULL, 0, 0,
     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
   );
   UPDATE repositories SET enabled=FALSE, status='unavailable', updated_at=CURRENT_TIMESTAMP
     WHERE id='$REPO_ID';
   COMMIT;" >/dev/null
wait_job_status "$JOB3" rejected 40
[[ "$(job_value "$JOB3" last_error_code)" == "repository_trust_revoked" ]]
if docker container inspect "ai-orchestra-runner-${JOB3//-/}" >/dev/null 2>&1; then
  echo "[FAIL] revoked repository reached disposable runner" >&2
  exit 1
fi
echo "[OK] revoked trust prevented runner side effect"

echo "[8/9] verify authoritative workspace never received runner writes"
docker run --rm --pull never --network none --read-only --user 10001:10001 \
  --entrypoint sh -e WS_ID -v "$VOLUME:/v:ro" "$IMAGE_ID" -lc '
    test "$(cat "/v/$WS_ID/marker")" = authoritative
    test ! -e "/v/$WS_ID/runner-manager-output.txt"
  '
echo "[OK] authoritative workspace unchanged"

echo "[9/9] verify Runner Manager runtime boundary"
inspect_json="$(docker inspect "${PROJECT}-runner-manager-1")"
python3 - "$inspect_json" <<'PY'
import json, sys
p=json.loads(sys.argv[1])[0]
host=p["HostConfig"]
assert host["ReadonlyRootfs"] is True, host
assert host["CapDrop"] == ["ALL"], host
assert p["Config"]["User"] == "10001:10001", p["Config"]["User"]
assert not any("docker.sock" in mount.get("Source", "") for mount in p.get("Mounts", [])), p.get("Mounts")
assert not any("task-workspaces" in mount.get("Name", "") for mount in p.get("Mounts", [])), p.get("Mounts")
PY
echo "[OK] G3.2 durable Runner Manager smoke passed"
