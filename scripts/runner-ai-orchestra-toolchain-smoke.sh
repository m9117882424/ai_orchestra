#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TAG="ai-orchestra/runner-toolchain-smoke:${GITHUB_RUN_ID:-$$}"

cleanup() {
  set +e
  docker image rm -f "$TAG" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[1/4] build AI Orchestra offline runner profile"
docker build --pull -t "$TAG" -f runner/Dockerfile.ai-orchestra . >/dev/null
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$TAG")"
[[ "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
echo "[OK] image=$IMAGE_ID"

echo "[2/4] verify immutable runner entrypoint"
[[ "$(docker image inspect --format '{{json .Config.Entrypoint}}' "$IMAGE_ID")" == '["python3","/opt/ai-orchestra-runner/entrypoint.py"]' ]]

echo "[3/4] verify locked Python test toolchain without runtime network"
docker run --rm --network none --entrypoint python3 "$IMAGE_ID" -m pytest --version | grep -q '^pytest '
docker run --rm --network none --entrypoint python3 "$IMAGE_ID" -c 'import fastapi, pytest, sqlalchemy; print("toolchain-ok")' | grep -q '^toolchain-ok$'

echo "[4/4] verify runner profile marker and non-root identity"
docker run --rm --network none --entrypoint python3 "$IMAGE_ID" -c 'import os; assert os.environ["AI_ORCHESTRA_RUNNER_PROFILE"] == "ai-orchestra-python"; assert os.getuid() == 10001'

echo "[OK] AI Orchestra offline runner toolchain smoke passed"
