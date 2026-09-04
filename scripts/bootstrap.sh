#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

mkdir -p runtime repos worktrees backups data/opencode data/state

random_secret() {
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
}

if [[ ! -f .env ]]; then
  cp .env.example .env
  sed -i "s|CHANGE_ME_LONG_RANDOM_PASSWORD|$(random_secret)|" .env
  sed -i "s|CHANGE_ME_MANAGER_PASSWORD|$(random_secret)|" .env
  sed -i "s|CHANGE_ME_DATABASE_PASSWORD|$(random_secret)|" .env
  sed -i "s|CHANGE_ME_MODEL_ROUTER_MASTER_KEY|sk-admin-$(random_secret)|" .env
  sed -i "s|CHANGE_ME_MODEL_ROUTER_CLIENT_KEY|sk-client-$(random_secret)|" .env
  echo "Создан .env с отдельными паролями интерфейсов, БД и router credentials."
else
  echo ".env уже существует — файл сохранен без перезаписи."
  if ! grep -q '^MODEL_ROUTER_MASTER_KEY=' .env; then
    printf '\nMODEL_ROUTER_MASTER_KEY=sk-admin-%s\n' "$(random_secret)" >> .env
  fi
  if ! grep -q '^MODEL_ROUTER_CLIENT_KEY=' .env; then
    printf 'MODEL_ROUTER_CLIENT_KEY=sk-client-%s\n' "$(random_secret)" >> .env
  fi
  if ! grep -q '^MODEL_GATEWAY_PORT=' .env; then
    echo 'MODEL_GATEWAY_PORT=18089' >> .env
  fi
  if ! grep -q '^LITELLM_VERSION=' .env; then
    echo 'LITELLM_VERSION=1.99.0' >> .env
  fi
fi
chmod 600 .env

if [[ ! -f .env.providers ]]; then
  cp .env.providers.example .env.providers
  echo "Создан .env.providers. В него помещаются только ключи AI-провайдеров."
else
  echo ".env.providers уже существует — файл сохранен без перезаписи."
fi
chmod 600 .env.providers

if grep -Eq '^(AITUNNEL_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|GOOGLE_GENERATIVE_AI_API_KEY)=' .env; then
  echo "[WARN] В старом .env обнаружены provider-переменные."
  echo "[WARN] Перенесите их значения в .env.providers; OpenCode их больше не получает."
fi

key_mode="$(grep -E '^KEY_MODE=' .env | tail -1 | cut -d= -f2- || true)"
case "${key_mode:-shared}" in
  shared|separate) ;;
  *)
    echo "[WARN] Некорректный KEY_MODE=${key_mode:-}; установлен shared."
    if grep -q '^KEY_MODE=' .env; then
      sed -i 's/^KEY_MODE=.*/KEY_MODE=shared/' .env
    else
      echo 'KEY_MODE=shared' >> .env
    fi
    key_mode=shared
    ;;
esac

cp config/opencode.gateway.json runtime/opencode.json
cp "config/model-router.${key_mode:-shared}.yaml" runtime/model-router.yaml

python3 -m json.tool runtime/opencode.json >/dev/null

echo
echo "Инициализация завершена."
echo "1. Заполните только нужные provider-ключи в .env.providers"
echo "2. Выполните: make preflight"
echo "3. Выполните: make build && make up && make smoke"
