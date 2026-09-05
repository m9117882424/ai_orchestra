#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

baseline_sql="$(mktemp /tmp/ai-orchestra-baseline-0001.XXXXXX.sql)"

cleanup() {
  docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$project_root/backups"
  rm -f "$baseline_sql"
}
trap cleanup EXIT

wait_postgres() {
  local container_id
  container_id="$(docker compose ps -q postgres)"
  if [[ -z "$container_id" ]]; then
    echo "[FAIL] PostgreSQL container was not created" >&2
    return 1
  fi

  local init_complete=0
  for _ in $(seq 1 60); do
    if [[ "$(docker inspect "$container_id" --format '{{.State.Running}}' 2>/dev/null || true)" != "true" ]]; then
      echo "[FAIL] PostgreSQL stopped during CI initialization" >&2
      docker compose logs postgres >&2 || true
      return 1
    fi
    if docker compose logs --no-color postgres 2>&1 | grep -Fq 'PostgreSQL init process complete; ready for start up.'; then
      init_complete=1
      break
    fi
    sleep 1
  done
  if [[ "$init_complete" != "1" ]]; then
    echo "[FAIL] PostgreSQL CI initialization did not complete" >&2
    docker compose logs postgres >&2 || true
    return 1
  fi

  for _ in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U ai_orchestra -d ai_orchestra >/dev/null 2>&1 \
      && [[ "$(docker compose exec -T postgres psql -U ai_orchestra -d ai_orchestra -Atc 'SELECT 1' 2>/dev/null || true)" == "1" ]]; then
      return 0
    fi
    sleep 1
  done
  echo "[FAIL] PostgreSQL final CI database did not become ready" >&2
  docker compose logs postgres >&2 || true
  return 1
}

echo "[INFO] Creating exact historical 0001 PostgreSQL source database"
docker compose up -d postgres >/dev/null
wait_postgres

# Generate the historical revision through Alembic itself, but in offline SQL mode.
# Applying that SQL with psql inside the PostgreSQL container keeps this fixture
# independent of cross-container password authentication while still proving that
# the source schema is exactly what revision 0001 declares.
docker compose run --rm --no-deps \
  -e CONTROL_PLANE_ENVIRONMENT=test \
  -e CONTROL_PLANE_DATABASE_URL=postgresql+psycopg://ai_orchestra:offline-only@postgres:5432/ai_orchestra \
  control-plane python -m alembic -c alembic.ini upgrade 20260904_0001 --sql \
  > "$baseline_sql"

if [[ ! -s "$baseline_sql" ]]; then
  echo "[FAIL] Alembic did not emit SQL for historical revision 20260904_0001" >&2
  exit 1
fi

docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  < "$baseline_sql" >/dev/null

source_revision="$(docker compose exec -T postgres psql -U ai_orchestra -d ai_orchestra -Atc \
  'SELECT version_num FROM alembic_version LIMIT 1')"
if [[ "$source_revision" != "20260904_0001" ]]; then
  echo "[FAIL] Expected historical source revision 20260904_0001, got: $source_revision" >&2
  exit 1
fi

# Production before Alembic adoption could contain the baseline shape without a
# marker. Reproduce that exact state so restore exercises fail-closed recognition:
# verify 0001 shape -> stamp 0001 -> upgrade through lease 0002 to dispatch 0003.
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c 'DROP TABLE alembic_version' >/dev/null

if docker compose exec -T postgres psql -U ai_orchestra -d ai_orchestra -Atc \
  "SELECT to_regclass('public.alembic_version') IS NOT NULL" | grep -qx t; then
  echo "[FAIL] CI source was expected to be unversioned after baseline marker removal" >&2
  exit 1
fi

docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra <<'SQL' >/dev/null
INSERT INTO audit_events (id, actor, action, entity_type, entity_id, details, created_at)
VALUES ('dr-smoke-marker', 'ci', 'backup_restore_smoke', 'test', 'dr-smoke-marker', '{}'::jsonb, NOW());
SQL

echo "[INFO] Creating and verifying historical CI backup"
bash ./scripts/backup.sh
archive="$(find "$project_root/backups" -maxdepth 1 -type f -name 'ai-orchestra-*.tar.gz' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"
bash ./scripts/verify-backup.sh "$archive"

echo "[INFO] Executing clean restore + historical adoption drill"
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
assert payload["post_migration_revision"] == "20260905_0003"
assert payload["restored_table_counts"].get("audit_events", 0) >= 1
assert payload["restored_table_counts"].get("alembic_version", 0) == 1
assert payload["observed_restore_rto_seconds"] >= 0
assert payload["observed_backup_age_seconds"] >= 0
print("[OK] Historical 0001 backup was restored, adopted to 0003 and retained the seeded audit marker")
PY

echo "[OK] Backup/restore smoke passed"
