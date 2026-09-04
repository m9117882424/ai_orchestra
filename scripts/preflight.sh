#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

failures=0
pass() { printf '[OK] %s\n' "$1"; }
fail() { printf '[FAIL] %s\n' "$1"; failures=$((failures + 1)); }

for command_name in docker python3; do
  if command -v "$command_name" >/dev/null 2>&1; then
    pass "Команда доступна: $command_name"
  else
    fail "Не найдена команда: $command_name"
  fi
done

if docker compose version >/dev/null 2>&1; then
  pass "Docker Compose v2 доступен"
else
  fail "Docker Compose v2 недоступен"
fi

for secret_file in .env .env.providers; do
  if [[ -f "$secret_file" ]]; then
    pass "$secret_file существует"
    mode="$(stat -c '%a' "$secret_file" 2>/dev/null || true)"
    if [[ "$mode" == "600" ]]; then
      pass "$secret_file имеет права 600"
    else
      fail "$secret_file должен иметь права 600 (сейчас: ${mode:-unknown})"
    fi
  else
    fail "$secret_file не найден; выполните make init"
  fi
done

if [[ -f runtime/opencode.json ]] && python3 -m json.tool runtime/opencode.json >/dev/null; then
  pass "Активная конфигурация OpenCode валидна"
else
  fail "runtime/opencode.json отсутствует или содержит ошибку"
fi

if [[ -s runtime/model-router.yaml ]]; then
  pass "Конфигурация Model Router существует"
else
  fail "runtime/model-router.yaml отсутствует"
fi

if [[ -f .env && -f .env.providers ]]; then
  if grep -Eq '^(AITUNNEL_API_KEY|AITUNNEL_BASE_URL|OPENAI_API_KEY|ANTHROPIC_API_KEY|GOOGLE_GENERATIVE_AI_API_KEY)=' .env; then
    fail "Provider credentials/config обнаружены в .env; перенесите строки в .env.providers и удалите их из .env"
  else
    pass "Provider credentials изолированы от .env"
  fi

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
  MODEL_ROUTER_CLIENT_KEY="${MODEL_ROUTER_CLIENT_KEY:-}"

  case "${KEY_MODE:-}" in
    shared)
      if [[ -n "${AITUNNEL_API_KEY:-}" ]]; then
        pass "AITunnel-ключ задан в provider scope"
      else
        fail "Для shared заполните AITUNNEL_API_KEY в .env.providers"
      fi
      ;;
    separate)
      if [[ -n "${OPENAI_API_KEY:-}" ]]; then
        pass "OpenAI-ключ задан"
      else
        fail "Для separate заполните OPENAI_API_KEY в .env.providers"
      fi
      if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
        pass "Anthropic-ключ задан"
      else
        fail "Для separate заполните ANTHROPIC_API_KEY в .env.providers"
      fi
      if [[ -n "${GOOGLE_GENERATIVE_AI_API_KEY:-}" ]]; then
        pass "Google-ключ задан"
      else
        fail "Для separate заполните GOOGLE_GENERATIVE_AI_API_KEY в .env.providers"
      fi
      ;;
    *)
      fail "KEY_MODE должен быть shared или separate"
      ;;
  esac

  if [[ "${KEY_MODE:-}" == "shared" || "${KEY_MODE:-}" == "separate" ]]; then
    if cmp -s runtime/model-router.yaml "config/model-router.${KEY_MODE}.yaml"; then
      pass "runtime/model-router.yaml соответствует KEY_MODE=${KEY_MODE}"
    else
      fail "runtime/model-router.yaml не соответствует KEY_MODE=${KEY_MODE}; выполните ./scripts/switch-key-mode.sh ${KEY_MODE} --no-restart или make init"
    fi
  fi

  if cmp -s runtime/opencode.json config/opencode.gateway.json; then
    pass "runtime/opencode.json соответствует gateway-конфигурации"
  else
    fail "runtime/opencode.json устарел; выполните make init"
  fi

  if [[ "${#OPENCODE_SERVER_PASSWORD}" -ge 20 && "$OPENCODE_SERVER_PASSWORD" != "CHANGE_ME_LONG_RANDOM_PASSWORD" ]]; then
    pass "Пароль OpenCode задан"
  else
    fail "OPENCODE_SERVER_PASSWORD должен содержать не менее 20 символов"
  fi

  if [[ "${#CONTROL_PLANE_SERVER_PASSWORD}" -ge 20 && "$CONTROL_PLANE_SERVER_PASSWORD" != "CHANGE_ME_MANAGER_PASSWORD" ]]; then
    pass "Пароль кабинета руководителя задан"
  else
    fail "CONTROL_PLANE_SERVER_PASSWORD должен содержать не менее 20 символов"
  fi

  if [[ "${#CONTROL_PLANE_DB_PASSWORD}" -ge 20 && "$CONTROL_PLANE_DB_PASSWORD" != "CHANGE_ME_DATABASE_PASSWORD" ]]; then
    pass "Пароль PostgreSQL кабинета задан"
  else
    fail "CONTROL_PLANE_DB_PASSWORD должен содержать не менее 20 символов"
  fi

  if [[ "${#MODEL_ROUTER_MASTER_KEY}" -ge 32 && "$MODEL_ROUTER_MASTER_KEY" == sk-admin-* && "$MODEL_ROUTER_MASTER_KEY" != "CHANGE_ME_MODEL_ROUTER_MASTER_KEY" ]]; then
    pass "Admin key Model Router задан"
  else
    fail "MODEL_ROUTER_MASTER_KEY должен начинаться с sk-admin- и быть не короче 32 символов"
  fi

  if [[ "${#MODEL_ROUTER_CLIENT_KEY}" -ge 32 && "$MODEL_ROUTER_CLIENT_KEY" == sk-client-* && "$MODEL_ROUTER_CLIENT_KEY" != "CHANGE_ME_MODEL_ROUTER_CLIENT_KEY" ]]; then
    pass "Inference client key задан"
  else
    fail "MODEL_ROUTER_CLIENT_KEY должен начинаться с sk-client- и быть не короче 32 символов"
  fi

  if [[ "$MODEL_ROUTER_MASTER_KEY" != "$MODEL_ROUTER_CLIENT_KEY" ]]; then
    pass "Router admin/client credentials разделены"
  else
    fail "Router admin/client credentials не должны совпадать"
  fi
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
forbidden={"AITUNNEL_API_KEY","OPENAI_API_KEY","ANTHROPIC_API_KEY","GOOGLE_GENERATIVE_AI_API_KEY","CONTROL_PLANE_DB_PASSWORD","CONTROL_PLANE_SERVER_PASSWORD","MODEL_ROUTER_MASTER_KEY"}
env=set((op.get("environment") or {}).keys())
bad=sorted(env & forbidden)
if bad:
    raise SystemExit("OpenCode receives forbidden secrets: " + ", ".join(bad))
assert "MODEL_ROUTER_CLIENT_KEY" in env

def nets(name):
    n=services[name].get("networks") or {}
    return set(n.keys() if isinstance(n, dict) else n)

assert nets("postgres")=={"control-db"}, nets("postgres")
assert nets("control-plane")=={"control-db"}, nets("control-plane")
assert nets("opencode")=={"model-net"}, nets("opencode")
assert nets("model-gateway")=={"model-net","router-backend"}, nets("model-gateway")
assert nets("model-router")=={"router-backend","provider-egress"}, nets("model-router")
assert not (nets("opencode") & nets("model-router")), "OpenCode must not share a network with router admin service"
' >/dev/null; then
  pass "Секреты, admin router и Docker-сети изолированы от OpenCode"
else
  fail "Нарушена изоляция OpenCode / Model Gateway / Model Router / control-plane"
fi

if (( failures > 0 )); then
  echo
  echo "Preflight: обнаружено ошибок: $failures"
  exit 1
fi

echo
echo "Preflight: контур готов к сборке."
