#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

mode="${1:-}"
restart="${2:---restart}"

case "$mode" in
  shared|separate) ;;
  *)
    echo "Использование: $0 shared|separate [--restart|--no-restart]" >&2
    exit 2
    ;;
esac
[[ "$restart" == "--restart" || "$restart" == "--no-restart" ]] || { echo "Неизвестный аргумент: $restart" >&2; exit 2; }

[[ -f .env && -f .env.providers ]] || { echo "[FAIL] Выполните make init" >&2; exit 1; }

set -a
# shellcheck disable=SC1091
source .env
# shellcheck disable=SC1091
source .env.providers
set +a

if [[ "$mode" == "shared" ]]; then
  [[ -n "${AITUNNEL_API_KEY:-}" ]] || { echo "[FAIL] AITUNNEL_API_KEY не задан в .env.providers" >&2; exit 1; }
else
  for name in OPENAI_API_KEY ANTHROPIC_API_KEY GOOGLE_GENERATIVE_AI_API_KEY; do
    [[ -n "${!name:-}" ]] || { echo "[FAIL] $name не задан в .env.providers" >&2; exit 1; }
  done
fi

source_router="config/model-router.${mode}.yaml"
source_opencode="config/opencode.gateway.json"
[[ -s "$source_router" ]] || { echo "[FAIL] Не найден $source_router" >&2; exit 1; }
python3 -m json.tool "$source_opencode" >/dev/null

mkdir -p runtime
backup_dir="$(mktemp -d /tmp/ai-orchestra-switch.XXXXXX)"
cleanup() { find "$backup_dir" -depth -delete 2>/dev/null || true; }
trap cleanup EXIT

previous_mode="${KEY_MODE:-shared}"
[[ -f runtime/model-router.yaml ]] && cp runtime/model-router.yaml "$backup_dir/model-router.yaml"
[[ -f runtime/opencode.json ]] && cp runtime/opencode.json "$backup_dir/opencode.json"

cp "$source_router" runtime/model-router.yaml.tmp
mv runtime/model-router.yaml.tmp runtime/model-router.yaml
cp "$source_opencode" runtime/opencode.json.tmp
mv runtime/opencode.json.tmp runtime/opencode.json

set_mode() {
  local value="$1"
  if grep -q '^KEY_MODE=' .env; then
    sed -i "s/^KEY_MODE=.*/KEY_MODE=${value}/" .env
  else
    printf '\nKEY_MODE=%s\n' "$value" >> .env
  fi
}
set_mode "$mode"

echo "Маршрутизация подготовлена: $mode"

if [[ "$restart" == "--no-restart" ]]; then
  exit 0
fi

if ! command -v docker >/dev/null 2>&1 || ! docker compose ps -q model-router 2>/dev/null | grep -q .; then
  echo "Контур еще не запущен; новый режим применится при следующем make up."
  exit 0
fi

rollback() {
  echo "[ROLLBACK] Возвращаю предыдущую маршрутизацию: $previous_mode" >&2
  [[ -f "$backup_dir/model-router.yaml" ]] && cp "$backup_dir/model-router.yaml" runtime/model-router.yaml
  [[ -f "$backup_dir/opencode.json" ]] && cp "$backup_dir/opencode.json" runtime/opencode.json
  set_mode "$previous_mode"
  docker compose up -d --force-recreate model-router model-gateway >/dev/null 2>&1 || true
}

docker compose up -d --force-recreate model-router model-gateway

router_id="$(docker compose ps -q model-router)"
gateway_id="$(docker compose ps -q model-gateway)"
healthy=0
for _ in $(seq 1 50); do
  router_state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$router_id" 2>/dev/null || true)"
  gateway_state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$gateway_id" 2>/dev/null || true)"
  if [[ "$router_state" == "healthy" && "$gateway_state" == "healthy" ]]; then
    healthy=1
    break
  fi
  [[ "$router_state" == "unhealthy" || "$router_state" == "exited" || "$gateway_state" == "unhealthy" || "$gateway_state" == "exited" ]] && break
  sleep 2
done

if (( healthy == 0 )); then
  echo "[FAIL] Model Router/Gateway не стали healthy" >&2
  rollback
  exit 1
fi

export KEY_MODE="$mode"
if ! python3 scripts/model_router_smoke.py --mode "$mode"; then
  echo "[FAIL] Новые provider API/model routes не прошли smoke test" >&2
  rollback
  exit 1
fi

docker compose up -d --force-recreate opencode
echo "[OK] Режим $mode активирован; provider smoke test пройден."
