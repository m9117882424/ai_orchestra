#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

mkdir -p runtime repos worktrees backups data/opencode data/state

if [[ ! -f .env ]]; then
  cp .env.example .env
  generated_opencode_password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  generated_manager_password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  generated_database_password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  sed -i "s|CHANGE_ME_LONG_RANDOM_PASSWORD|${generated_opencode_password}|" .env
  sed -i "s|CHANGE_ME_MANAGER_PASSWORD|${generated_manager_password}|" .env
  sed -i "s|CHANGE_ME_DATABASE_PASSWORD|${generated_database_password}|" .env
  chmod 600 .env
  echo "Создан .env с отдельными паролями OpenCode, кабинета и БД."
else
  chmod 600 .env
  echo ".env уже существует — файл сохранен без перезаписи."
  if ! grep -q '^CONTROL_PLANE_SERVER_USERNAME=' .env; then
    {
      echo
      echo '# Кабинет руководителя (добавлено при обновлении)'
      echo 'CONTROL_PLANE_SERVER_USERNAME=manager'
      printf 'CONTROL_PLANE_SERVER_PASSWORD=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
      echo 'CONTROL_PLANE_PORT=8088'
      printf 'CONTROL_PLANE_DB_PASSWORD=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
      echo 'CONTROL_PLANE_DEFAULT_MONTHLY_BUDGET=25000'
      echo 'OPENCODE_PUBLIC_URL=http://127.0.0.1:4096'
      echo 'BACKUP_RETENTION_DAYS=14'
    } >> .env
    echo "Добавлены недостающие настройки кабинета руководителя."
  fi
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
