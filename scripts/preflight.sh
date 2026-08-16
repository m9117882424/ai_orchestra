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

if [[ -f .env ]]; then
  pass ".env существует"
else
  fail ".env не найден; выполните make init"
fi

if [[ -f runtime/opencode.json ]] && python3 -m json.tool runtime/opencode.json >/dev/null; then
  pass "Активная конфигурация OpenCode валидна"
else
  fail "runtime/opencode.json отсутствует или содержит ошибку"
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a

  case "${KEY_MODE:-}" in
    shared)
      [[ -n "${AITUNNEL_API_KEY:-}" ]] && pass "AITunnel-ключ задан" || fail "Для shared заполните AITUNNEL_API_KEY"
      ;;
    separate)
      [[ -n "${OPENAI_API_KEY:-}" ]] && pass "OpenAI-ключ задан" || fail "Для separate заполните OPENAI_API_KEY"
      [[ -n "${ANTHROPIC_API_KEY:-}" ]] && pass "Anthropic-ключ задан" || fail "Для separate заполните ANTHROPIC_API_KEY"
      [[ -n "${GOOGLE_GENERATIVE_AI_API_KEY:-}" ]] && pass "Google-ключ задан" || fail "Для separate заполните GOOGLE_GENERATIVE_AI_API_KEY"
      ;;
    *)
      fail "KEY_MODE должен быть shared или separate"
      ;;
  esac

  if [[ -n "${OPENCODE_SERVER_PASSWORD:-}" && "${OPENCODE_SERVER_PASSWORD}" != "CHANGE_ME_LONG_RANDOM_PASSWORD" ]]; then
    pass "Пароль веб-интерфейса задан"
  else
    fail "Замените OPENCODE_SERVER_PASSWORD"
  fi

  control_plane_password="${CONTROL_PLANE_SERVER_PASSWORD:-}"
  if [[ "${#control_plane_password}" -ge 20 && "$control_plane_password" != "CHANGE_ME_MANAGER_PASSWORD" ]]; then
    pass "Пароль кабинета руководителя задан"
  else
    fail "CONTROL_PLANE_SERVER_PASSWORD должен содержать не менее 20 символов; для обновления можно повторить make init"
  fi

  control_plane_db_password="${CONTROL_PLANE_DB_PASSWORD:-}"
  if [[ "${#control_plane_db_password}" -ge 20 && "$control_plane_db_password" != "CHANGE_ME_DATABASE_PASSWORD" ]]; then
    pass "Пароль PostgreSQL кабинета задан"
  else
    fail "CONTROL_PLANE_DB_PASSWORD должен содержать не менее 20 символов; для обновления можно повторить make init"
  fi
fi

if docker compose config --quiet >/dev/null 2>&1; then
  pass "docker-compose.yml валиден"
else
  fail "docker compose config завершился с ошибкой"
fi

if (( failures > 0 )); then
  echo
  echo "Preflight: обнаружено ошибок: $failures"
  exit 1
fi

echo
echo "Preflight: контур готов к сборке."
