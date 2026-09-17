#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

running_services="$(docker compose ps --status running --services)"
if ! grep -qx postgres <<< "$running_services"; then
  echo "[FAIL] PostgreSQL не запущен. Сначала: docker compose up -d postgres" >&2
  exit 1
fi

# Include the optional Runner Manager: it writes runner_jobs even when the base
# Compose services are stopped. Never migrate underneath a lifecycle writer.
for writer in control-plane execution-worker repo-manager workspace-manager opencode runner-manager; do
  if grep -qx "$writer" <<< "$running_services"; then
    echo "[FAIL] Остановите writer $writer перед миграцией (Runner Manager использует отдельный Compose overlay)." >&2
    exit 1
  fi
done

echo "[INFO] Создаю обязательную резервную копию перед изменением schema revision"
bash ./scripts/backup.sh

echo "[INFO] Запускаю управляемую миграцию Control Plane"
docker compose run --rm -T --no-deps control-plane python -m app.schema_cli migrate

echo "[INFO] Проверяю итоговую revision и physical schema shape"
docker compose run --rm -T --no-deps control-plane python -m app.schema_cli check

echo "[OK] Control Plane schema готова"
