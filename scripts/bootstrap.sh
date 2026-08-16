#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

mkdir -p runtime repos data/opencode data/state

if [[ ! -f .env ]]; then
  cp .env.example .env
  generated_password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  sed -i "s|CHANGE_ME_LONG_RANDOM_PASSWORD|${generated_password}|" .env
  chmod 600 .env
  echo "Создан .env с новым паролем веб-интерфейса."
else
  chmod 600 .env
  echo ".env уже существует — файл сохранен без перезаписи."
fi

if [[ ! -f runtime/opencode.json ]]; then
  cp config/opencode.shared.json runtime/opencode.json
fi

python3 -m json.tool runtime/opencode.json >/dev/null

echo
echo "Инициализация завершена."
echo "1. Заполните ключи в .env"
echo "2. Выполните: make preflight"
echo "3. Выполните: make build && make up"

