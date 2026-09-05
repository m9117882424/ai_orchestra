#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

cleanup() {
  docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$project_root/backups"
}
trap cleanup EXIT

wait_postgres() {
  for _ in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U ai_orchestra -d ai_orchestra >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "[FAIL] PostgreSQL did not become ready" >&2
  docker compose logs postgres >&2 || true
  return 1
}

echo "[INFO] Creating legacy unversioned CI source database"
docker compose up -d postgres >/dev/null
wait_postgres

docker compose run --rm --no-deps \
  -e CONTROL_PLANE_ENVIRONMENT=test \
  control-plane python - <<'PY'
from app.database_base import Base
from app.database_engine import create_configured_engine
import app.models  # noqa: F401

engine = create_configured_engine()
Base.metadata.create_all(engine)
print("[OK] Legacy unversioned schema created")
PY

if docker compose exec -T postgres psql -U ai_orchestra -d ai_orchestra -Atc \
  "SELECT to_regclass('public.alembic_version') IS NOT NULL" | grep -qx t; then
  echo "[FAIL] CI source was expected to be unversioned" >&2
  exit 1
fi

docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra <<'SQL' >/dev/null
INSERT INTO audit_events (id, actor, action, entity_type, entity_id, details, created_at)
VALUES ('dr-smoke-marker', 'ci', 'backup_restore_smoke', 'test', 'dr-smoke-marker', '{}'::jsonb, NOW());
SQL

echo "[INFO] Creating and verifying legacy CI backup"
bash ./scripts/backup.sh
archive="$(find "$project_root/backups" -maxdepth 1 -type f -name 'ai-orchestra-*.tar.gz' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"
bash ./scripts/verify-backup.sh "$archive"

echo "[INFO] Executing clean restore + adoption drill"
bash ./scripts/restore-drill.sh "$archive"
evidence="$(find "$project_root/backups/drills" -maxdepth 1 -type f -name 'restore-drill-*.json' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"

python3 - "$archive" "$evidence" <<'PY'
import hashlib
import json
import pathlib
import sys

archive = pathlib.Path(sys.argv[1]).resolve()
evidence = pathlib.Path(sys.argv[2])
payload = json.loads(evidence.read_text(encoding="utf-8"))
sha = hashlib.sha256(archive.read_bytes()).hexdigest()
assert payload["result"] == "success"
assert payload["source_backup_sha256"] == sha
assert payload["pre_migration_revision"] == "unversioned"
assert payload["post_migration_revision"] == "20260904_0001"
assert payload["restored_table_counts"].get("audit_events", 0) >= 1
assert payload["observed_restore_rto_seconds"] >= 0
assert payload["observed_backup_age_seconds"] >= 0
print("[OK] Legacy backup was restored, adopted and retained the seeded audit marker")
PY

echo "[OK] Backup/restore smoke passed"
