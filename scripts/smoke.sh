#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

set -a
# shellcheck disable=SC1091
source .env
set +a

health_url="http://127.0.0.1:${OPENCODE_PORT:-4096}/global/health"
curl -fsS \
  -u "${OPENCODE_SERVER_USERNAME}:${OPENCODE_SERVER_PASSWORD}" \
  "$health_url" >/dev/null
echo "[OK] OpenCode Web отвечает"

control_plane_url="http://127.0.0.1:${CONTROL_PLANE_PORT:-8088}"
curl -fsS "${control_plane_url}/health" >/dev/null
curl -fsS \
  -u "${CONTROL_PLANE_SERVER_USERNAME}:${CONTROL_PLANE_SERVER_PASSWORD}" \
  "${control_plane_url}/api/summary" >/dev/null
echo "[OK] Кабинет руководителя и PostgreSQL отвечают"

if [[ "${KEY_MODE:-shared}" == "shared" ]]; then
  export AITUNNEL_API_KEY
  python3 scripts/aitunnel_smoke_test.py --model "${AITUNNEL_SMOKE_MODEL:-gpt-4o-mini}"
else
  echo "[SKIP] AITunnel smoke test: активен режим separate"
fi
