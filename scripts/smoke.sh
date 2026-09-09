#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

set -a
# shellcheck disable=SC1091
source .env
set +a

gateway_health="http://127.0.0.1:${MODEL_GATEWAY_PORT:-18089}/health"
curl -fsS "$gateway_health" >/dev/null
echo "[OK] Inference Model Gateway отвечает"

python3 scripts/model_router_smoke.py \
  --base-url "http://127.0.0.1:${MODEL_GATEWAY_PORT:-18089}/v1" \
  --mode "${KEY_MODE:-shared}"

health_url="http://127.0.0.1:${OPENCODE_PORT:-4096}/global/health"
curl -fsS -u "${OPENCODE_SERVER_USERNAME}:${OPENCODE_SERVER_PASSWORD}" "$health_url" >/dev/null
echo "[OK] OpenCode Web отвечает"

control_plane_url="http://127.0.0.1:${CONTROL_PLANE_PORT:-8088}"
curl -fsS "${control_plane_url}/health" >/dev/null
curl -fsS -u "${CONTROL_PLANE_SERVER_USERNAME}:${CONTROL_PLANE_SERVER_PASSWORD}" "${control_plane_url}/api/summary" >/dev/null
echo "[OK] Кабинет руководителя и PostgreSQL отвечают"

worker_id="$(docker compose ps --status running -q execution-worker)"
if [[ -z "$worker_id" ]]; then
  echo "[FAIL] Execution Worker не запущен" >&2
  docker compose logs --tail=100 execution-worker >&2 || true
  exit 1
fi
worker_health="$(docker inspect --format '{{.State.Health.Status}}' "$worker_id" 2>/dev/null || true)"
if [[ "$worker_health" != "healthy" ]]; then
  echo "[FAIL] Execution Worker не прошёл liveness: $worker_health" >&2
  docker compose logs --tail=100 execution-worker >&2 || true
  exit 1
fi
echo "[OK] Execution Worker запущен и healthy"

repo_manager_id="$(docker compose ps --status running -q repo-manager)"
if [[ -z "$repo_manager_id" ]]; then
  echo "[FAIL] Repo Manager не запущен" >&2
  docker compose logs --tail=100 repo-manager >&2 || true
  exit 1
fi
repo_manager_health="$(docker inspect --format '{{.State.Health.Status}}' "$repo_manager_id" 2>/dev/null || true)"
if [[ "$repo_manager_health" != "healthy" ]]; then
  echo "[FAIL] Repo Manager не прошёл liveness: $repo_manager_health" >&2
  docker compose logs --tail=100 repo-manager >&2 || true
  exit 1
fi
echo "[OK] Repo Manager запущен и healthy"

echo "[OK] Smoke test завершен"
