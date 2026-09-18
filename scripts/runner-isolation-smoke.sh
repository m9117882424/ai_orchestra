#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TAG="ai-orchestra/runner-isolation-smoke:${GITHUB_RUN_ID:-$$}"
VOLUME="ai-orchestra-runner-smoke-${GITHUB_RUN_ID:-$$}"
TMP_DIR="$(mktemp -d)"
SOCKET="$TMP_DIR/runnerd.sock"
LOG="$TMP_DIR/runnerd.log"
RUNNER_PID=""

cleanup() {
  set +e
  if [[ -n "$RUNNER_PID" ]]; then
    kill "$RUNNER_PID" 2>/dev/null || true
    wait "$RUNNER_PID" 2>/dev/null || true
  fi
  for request_id in "${REQ_A:-}" "${REQ_NET:-}" "${REQ_BAD:-}" "${REQ_SNAPSHOT:-}" "${REQ_TIME:-}"; do
    if [[ -n "$request_id" ]]; then
      docker rm -f "ai-orchestra-runner-${request_id//-/}" >/dev/null 2>&1 || true
    fi
  done
  docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true
  docker image rm -f "$TAG" >/dev/null 2>&1 || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

echo "[1/9] build immutable runner image"
docker build --pull -t "$TAG" -f runner/Dockerfile runner >/dev/null
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$TAG")"
[[ "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
echo "[OK] image=$IMAGE_ID"

echo "[2/9] create isolated test volume"
docker volume create "$VOLUME" >/dev/null
WS_A="$(python3 -c 'import uuid; print(uuid.uuid4())')"
WS_B="$(python3 -c 'import uuid; print(uuid.uuid4())')"
EX_A="$(python3 -c 'import uuid; print(uuid.uuid4())')"
REPO_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
REQ_A="$(python3 -c 'import uuid; print(uuid.uuid4())')"
DIGEST="$(python3 -c 'print("d"*64)')"

export WS_A WS_B EX_A DIGEST
docker run --rm --user 0:0 --entrypoint sh \
  -v "$VOLUME:/v" -e WS_A -e WS_B "$IMAGE_ID" -lc '
set -eu
for item in "$WS_A:A" "$WS_B:B"; do
  workspace_id="${item%%:*}"
  marker="${item##*:}"
  root="/v/$workspace_id"
  mkdir -p "$root"
  git -C "$root" init -q
  git -C "$root" config user.email smoke@example.invalid
  git -C "$root" config user.name smoke
  printf "%s\n" "$marker" > "$root/marker"
  git -C "$root" add marker
  GIT_AUTHOR_DATE=2026-09-16T00:00:00Z GIT_COMMITTER_DATE=2026-09-16T00:00:00Z \
    git -C "$root" commit -qm initial
  chown -R 10001:10001 "$root"
  chmod 700 "$root" "$root/.git"
done
'
COMMIT="$(docker run --rm --entrypoint git -v "$VOLUME:/v:ro" "$IMAGE_ID" -C "/v/$WS_A" rev-parse HEAD)"
export COMMIT
docker run --rm --user 0:0 --entrypoint python3 \
  -v "$VOLUME:/v" -e WS_A -e WS_B -e EX_A -e COMMIT -e DIGEST \
  "$IMAGE_ID" -c '
import json, os
from pathlib import Path
for key in ("WS_A", "WS_B"):
    workspace_id = os.environ[key]
    manifest = {
        "contract_version": 1,
        "workspace_id": workspace_id,
        "execution_id": os.environ["EX_A"],
        "base_commit": os.environ["COMMIT"],
        "preflight_digest": os.environ["DIGEST"],
        "workspace_path": f"/workspace/worktrees/managed/{workspace_id}",
    }
    path = Path("/v") / workspace_id / ".git" / "ai-orchestra-workspace.json"
    path.write_text(json.dumps(manifest))
    os.chown(path, 10001, 10001)
'
SNAPSHOT="$(docker run --rm --entrypoint python3 -v "$VOLUME:/v:ro" -e WS_A "$IMAGE_ID" -c '
import os, sys
from pathlib import Path
sys.path.insert(0, "/opt/ai-orchestra-runner")
from source_snapshot import source_snapshot_digest
print(source_snapshot_digest(Path("/v") / os.environ["WS_A"]))
')"
[[ "$SNAPSHOT" =~ ^[0-9a-f]{64}$ ]]

echo "[3/9] start host runner broker"
export RUNNERD_SOCKET_PATH="$SOCKET"
export RUNNERD_WORKSPACE_VOLUME="$VOLUME"
export RUNNERD_IMAGE_ID="$IMAGE_ID"
export RUNNERD_MAX_TIMEOUT_SECONDS=120
export RUNNERD_MAX_CONCURRENT=1
RUNNERD_ALLOW_NONROOT=1 python3 runner/runnerd.py serve >"$LOG" 2>&1 &
RUNNER_PID=$!
for _ in $(seq 1 50); do
  [[ -S "$SOCKET" ]] && break
  kill -0 "$RUNNER_PID" 2>/dev/null || { cat "$LOG" >&2; exit 1; }
  sleep 0.1
done
[[ -S "$SOCKET" ]]
python3 runner/runnerctl.py --socket "$SOCKET" health | grep -q '"status": "ok"'
echo "[OK] runnerd healthy"

echo "[4/9] verify exact workspace subpath, snapshot, and readonly rootfs"
RUN_OUT="$TMP_DIR/run-ok.json"
python3 runner/runnerctl.py --socket "$SOCKET" run \
  --repository-id "$REPO_ID" \
  --workspace-id "$WS_A" \
  --execution-id "$EX_A" \
  --base-commit "$COMMIT" \
  --preflight-digest "$DIGEST" \
  --source-snapshot-digest "$SNAPSHOT" \
  --request-id "$REQ_A" \
  --timeout 30 -- sh -lc \
  "test \"\$(cat marker)\" = A; test ! -e /workspace/../$WS_B; test ! -S /var/run/docker.sock; ! touch /rootfs-write; ! sh -c 'printf BAD > /source/marker'; printf runner-ok > runner-output.txt; test \"\$(cat runner-output.txt)\" = runner-ok" \
  >"$RUN_OUT"
python3 - "$RUN_OUT" <<'PY'
import json, sys
p=json.load(open(sys.argv[1]))
assert p["status"] == "completed" and p["exit_code"] == 0, p
assert p["cleanup_confirmed"] is True, p
PY
echo "[OK] exact workspace only; rootfs/socket boundaries hold"

echo "[5/9] verify network and secret isolation"
NET_OUT="$TMP_DIR/run-net.json"
REQ_NET="$(python3 -c 'import uuid; print(uuid.uuid4())')"
python3 runner/runnerctl.py --socket "$SOCKET" run \
  --repository-id "$REPO_ID" \
  --workspace-id "$WS_A" --execution-id "$EX_A" \
  --base-commit "$COMMIT" --preflight-digest "$DIGEST" \
  --request-id "$REQ_NET" --timeout 30 -- python3 -c \
  'import os,socket; bad=[k for k in os.environ if any(x in k.upper() for x in ("PASSWORD","TOKEN","SECRET","API_KEY","PRIVATE_KEY"))]; assert not bad,bad; s=socket.socket(); s.settimeout(1); assert s.connect_ex(("1.1.1.1",53)) != 0' \
  >"$NET_OUT"
python3 - "$NET_OUT" <<'PY'
import json, sys
p=json.load(open(sys.argv[1]))
assert p["status"] == "completed" and p["exit_code"] == 0, p
PY
echo "[OK] no inherited secrets and no network egress"

echo "[6/9] reject immutable binding mismatch"
BAD_OUT="$TMP_DIR/run-bad.json"
REQ_BAD="$(python3 -c 'import uuid; print(uuid.uuid4())')"
set +e
python3 runner/runnerctl.py --socket "$SOCKET" run \
  --repository-id "$REPO_ID" \
  --workspace-id "$WS_A" --execution-id "$EX_A" \
  --base-commit "$COMMIT" --preflight-digest "$(python3 -c 'print("0"*64)')" \
  --request-id "$REQ_BAD" --timeout 30 -- sh -c 'echo SHOULD_NOT_RUN' \
  >"$BAD_OUT"
BAD_RC=$?
set -e
[[ "$BAD_RC" -ne 0 ]]
python3 - "$BAD_OUT" <<'PY'
import json, sys
p=json.load(open(sys.argv[1]))
assert p["status"] == "failed" and p["exit_code"] != 0, p
assert "SHOULD_NOT_RUN" not in p["stdout"], p
assert "manifest mismatch" in p["stderr"], p
PY
echo "[OK] manifest mismatch fails before command execution"

echo "[7/9] reject exact source snapshot drift"
SNAP_OUT="$TMP_DIR/run-snapshot.json"
REQ_SNAPSHOT="$(python3 -c 'import uuid; print(uuid.uuid4())')"
docker run --rm --user 0:0 --entrypoint sh -v "$VOLUME:/v" -e WS_A "$IMAGE_ID" -lc \
  'printf "MUTATED\n" > "/v/$WS_A/marker"; chown 10001:10001 "/v/$WS_A/marker"'
set +e
python3 runner/runnerctl.py --socket "$SOCKET" run \
  --repository-id "$REPO_ID" \
  --workspace-id "$WS_A" --execution-id "$EX_A" \
  --base-commit "$COMMIT" --preflight-digest "$DIGEST" \
  --source-snapshot-digest "$SNAPSHOT" \
  --request-id "$REQ_SNAPSHOT" --timeout 30 -- sh -c 'echo SHOULD_NOT_RUN' \
  >"$SNAP_OUT"
SNAP_RC=$?
set -e
[[ "$SNAP_RC" -ne 0 ]]
python3 - "$SNAP_OUT" <<'PY'
import json, sys
p=json.load(open(sys.argv[1]))
assert p["status"] == "failed" and p["exit_code"] != 0, p
assert "SHOULD_NOT_RUN" not in p["stdout"], p
assert "source snapshot mismatch" in p["stderr"], p
PY
docker run --rm --user 0:0 --entrypoint sh -v "$VOLUME:/v" -e WS_A "$IMAGE_ID" -lc \
  'printf "A\n" > "/v/$WS_A/marker"; chown 10001:10001 "/v/$WS_A/marker"'
echo "[OK] stale source snapshot rejected before command execution"

echo "[8/9] enforce timeout and confirm cleanup"
TIME_OUT="$TMP_DIR/run-timeout.json"
REQ_TIME="$(python3 -c 'import uuid; print(uuid.uuid4())')"
set +e
python3 runner/runnerctl.py --socket "$SOCKET" run \
  --repository-id "$REPO_ID" \
  --workspace-id "$WS_A" --execution-id "$EX_A" \
  --base-commit "$COMMIT" --preflight-digest "$DIGEST" \
  --request-id "$REQ_TIME" --timeout 1 -- python3 -c 'import time; time.sleep(30)' \
  >"$TIME_OUT"
TIME_RC=$?
set -e
[[ "$TIME_RC" -ne 0 ]]
python3 - "$TIME_OUT" <<'PY'
import json, sys
p=json.load(open(sys.argv[1]))
assert p["status"] == "timed_out", p
assert p["cleanup_confirmed"] is True, p
assert p["exit_code"] is None, p
PY
if docker container inspect "ai-orchestra-runner-${REQ_TIME//-/}" >/dev/null 2>&1; then
  echo "[FAIL] timed-out runner container still exists" >&2
  exit 1
fi
echo "[OK] timeout is terminal failure and container cleanup confirmed"

echo "[9/9] verify workspace effect is scoped"
docker run --rm --pull never --network none --read-only \
  --user 10001:10001 --entrypoint sh \
  -e WS_A -e WS_B -v "$VOLUME:/v:ro" "$IMAGE_ID" -lc '
    test "$(cat "/v/$WS_A/marker")" = A
    test "$(cat "/v/$WS_B/marker")" = B
    test ! -e "/v/$WS_A/runner-output.txt"
    test ! -e "/v/$WS_B/runner-output.txt"
  '
for request_id in "$REQ_A" "$REQ_NET" "$REQ_BAD" "$REQ_SNAPSHOT" "$REQ_TIME"; do
  if docker container inspect "ai-orchestra-runner-${request_id//-/}" >/dev/null 2>&1; then
    echo "[FAIL] disposable runner container leaked: $request_id" >&2
    exit 1
  fi
done
echo "[OK] G3 runner isolation smoke passed"
