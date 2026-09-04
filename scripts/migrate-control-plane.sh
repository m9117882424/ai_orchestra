#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

if ! docker compose ps --status running --services | grep -qx postgres; then
  echo "[FAIL] PostgreSQL не запущен. Сначала: docker compose up -d postgres" >&2
  exit 1
fi

echo "[INFO] Создаю обязательную резервную копию перед изменением schema revision"
bash ./scripts/backup.sh

echo "[INFO] Запускаю управляемую миграцию Control Plane"
docker compose run --rm --no-deps control-plane python -m app.schema_cli migrate

echo "[INFO] Проверяю итоговую revision и physical schema shape"
docker compose run --rm --no-deps control-plane python -m app.schema_cli check

echo "[OK] Control Plane schema готова"
