#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

set -a
# shellcheck disable=SC1091
source .env
set +a

router_health="http://127.0.0.1:${MODEL_ROUTER_PORT:-18089}/health/liveliness"
curl -fsS "$router_health" >/dev/null
echo "[OK] Model Router отвечает"

python3 scripts/model_router_smoke.py \
  --base-url "http://127.0.0.1:${MODEL_ROUTER_PORT:-18089}/v1" \
  --mode "${KEY_MODE:-shared}"

health_url="http://127.0.0.1:${OPENCODE_PORT:-4096}/global/health"
curl -fsS -u "${OPENCODE_SERVER_USERNAME}:${OPENCODE_SERVER_PASSWORD}" "$health_url" >/dev/null
echo "[OK] OpenCode Web отвечает"

control_plane_url="http://127.0.0.1:${CONTROL_PLANE_PORT:-8088}"
curl -fsS "${control_plane_url}/health" >/dev/null
curl -fsS -u "${CONTROL_PLANE_SERVER_USERNAME}:${CONTROL_PLANE_SERVER_PASSWORD}" "${control_plane_url}/api/summary" >/dev/null
echo "[OK] Кабинет руководителя и PostgreSQL отвечают"

echo "[OK] Smoke test завершен"
