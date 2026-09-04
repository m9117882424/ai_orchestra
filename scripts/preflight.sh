#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

failures=0
pass() { printf '[OK] %s\n' "$1"; }
fail() { printf '[FAIL] %s\n' "$1"; failures=$((failures + 1)); }

for command_name in docker python3; do
  command -v "$command_name" >/dev/null 2>&1 && pass "Команда доступна: $command_name" || fail "Не найдена команда: $command_name"
done

docker compose version >/dev/null 2>&1 && pass "Docker Compose v2 доступен" || fail "Docker Compose v2 недоступен"

for secret_file in .env .env.providers; do
  if [[ -f "$secret_file" ]]; then
    pass "$secret_file существует"
    mode="$(stat -c '%a' "$secret_file" 2>/dev/null || true)"
    [[ "$mode" == "600" ]] && pass "$secret_file имеет права 600" || fail "$secret_file должен иметь права 600 (сейчас: ${mode:-unknown})"
  else
    fail "$secret_file не найден; выполните make init"
  fi
done

if [[ -f runtime/opencode.json ]] && python3 -m json.tool runtime/opencode.json >/dev/null; then
  pass "Активная конфигурация OpenCode валидна"
else
  fail "runtime/opencode.json отсутствует или содержит ошибку"
fi
[[ -s runtime/model-router.yaml ]] && pass "Конфигурация Model Router существует" || fail "runtime/model-router.yaml отсутствует"

if [[ -f .env && -f .env.providers ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  # shellcheck disable=SC1091
  source .env.providers
  set +a

  OPENCODE_SERVER_PASSWORD="${OPENCODE_SERVER_PASSWORD:-}"
  CONTROL_PLANE_SERVER_PASSWORD="${CONTROL_PLANE_SERVER_PASSWORD:-}"
  CONTROL_PLANE_DB_PASSWORD="${CONTROL_PLANE_DB_PASSWORD:-}"
  MODEL_ROUTER_MASTER_KEY="${MODEL_ROUTER_MASTER_KEY:-}"

  case "${KEY_MODE:-}" in
    shared)
      [[ -n "${AITUNNEL_API_KEY:-}" ]] && pass "AITunnel-ключ задан в provider scope" || fail "Для shared заполните AITUNNEL_API_KEY в .env.providers"
      ;;
    separate)
      [[ -n "${OPENAI_API_KEY:-}" ]] && pass "OpenAI-ключ задан" || fail "Для separate заполните OPENAI_API_KEY в .env.providers"
      [[ -n "${ANTHROPIC_API_KEY:-}" ]] && pass "Anthropic-ключ задан" || fail "Для separate заполните ANTHROPIC_API_KEY в .env.providers"
      [[ -n "${GOOGLE_GENERATIVE_AI_API_KEY:-}" ]] && pass "Google-ключ задан" || fail "Для separate заполните GOOGLE_GENERATIVE_AI_API_KEY в .env.providers"
      ;;
    *)
      fail "KEY_MODE должен быть shared или separate"
      ;;
  esac

  [[ "${#OPENCODE_SERVER_PASSWORD}" -ge 20 && "$OPENCODE_SERVER_PASSWORD" != "CHANGE_ME_LONG_RANDOM_PASSWORD" ]] \
    && pass "Пароль OpenCode задан" || fail "OPENCODE_SERVER_PASSWORD должен содержать не менее 20 символов"

  [[ "${#CONTROL_PLANE_SERVER_PASSWORD}" -ge 20 && "$CONTROL_PLANE_SERVER_PASSWORD" != "CHANGE_ME_MANAGER_PASSWORD" ]] \
    && pass "Пароль кабинета руководителя задан" || fail "CONTROL_PLANE_SERVER_PASSWORD должен содержать не менее 20 символов"

  [[ "${#CONTROL_PLANE_DB_PASSWORD}" -ge 20 && "$CONTROL_PLANE_DB_PASSWORD" != "CHANGE_ME_DATABASE_PASSWORD" ]] \
    && pass "Пароль PostgreSQL кабинета задан" || fail "CONTROL_PLANE_DB_PASSWORD должен содержать не менее 20 символов"

  [[ "${#MODEL_ROUTER_MASTER_KEY}" -ge 32 && "$MODEL_ROUTER_MASTER_KEY" == sk-* && "$MODEL_ROUTER_MASTER_KEY" != "CHANGE_ME_MODEL_ROUTER_KEY" ]] \
    && pass "Внутренний ключ Model Router задан" || fail "MODEL_ROUTER_MASTER_KEY должен начинаться с sk- и быть не короче 32 символов"
fi

if docker compose config --quiet >/dev/null 2>&1; then
  pass "docker-compose.yml валиден"
else
  fail "docker compose config завершился с ошибкой"
fi

# Verify OS-level isolation using Compose's resolved JSON without printing secrets.
if docker compose config --format json 2>/dev/null | python3 -c '
import json, sys
cfg=json.load(sys.stdin)
services=cfg["services"]
op=services["opencode"]
forbidden={"AITUNNEL_API_KEY","OPENAI_API_KEY","ANTHROPIC_API_KEY","GOOGLE_GENERATIVE_AI_API_KEY","CONTROL_PLANE_DB_PASSWORD","CONTROL_PLANE_SERVER_PASSWORD"}
env=set((op.get("environment") or {}).keys())
bad=sorted(env & forbidden)
if bad:
    raise SystemExit("OpenCode receives forbidden secrets: " + ", ".join(bad))
def nets(name):
    n=services[name].get("networks") or {}
    return set(n.keys() if isinstance(n, dict) else n)
assert nets("postgres")=={"control-db"}, nets("postgres")
assert nets("control-plane")=={"control-db"}, nets("control-plane")
assert nets("model-router")=={"model-net"}, nets("model-router")
assert nets("opencode")=={"model-net"}, nets("opencode")
' >/dev/null; then
  pass "Секреты и Docker-сети изолированы: OpenCode не видит provider/control-plane credentials"
else
  fail "Нарушена изоляция OpenCode / Model Router / control-plane"
fi

if (( failures > 0 )); then
  echo
  echo "Preflight: обнаружено ошибок: $failures"
  exit 1
fi

echo
echo "Preflight: контур готов к сборке."
