#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

cleanup() {
  docker compose down -v --remove-orphans >/dev/null 2>&1 || true
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

echo "[INFO] PostgreSQL legacy-adoption smoke"
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

docker compose run --rm --no-deps control-plane python -m app.schema_cli migrate
docker compose run --rm --no-deps control-plane python -m app.schema_cli check

echo "[INFO] PostgreSQL drift refusal smoke"
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c 'DROP INDEX ix_execution_runs_task_id;' >/dev/null

if docker compose run --rm --no-deps control-plane python -m app.schema_cli check; then
  echo "[FAIL] schema check accepted deliberately removed index" >&2
  exit 1
fi

echo "[INFO] PostgreSQL fresh-migration smoke"
docker compose down -v --remove-orphans >/dev/null
docker compose up -d postgres >/dev/null
wait_postgres

docker compose run --rm --no-deps control-plane python -m app.schema_cli migrate
docker compose run --rm --no-deps control-plane python -m app.schema_cli check

echo "[OK] PostgreSQL schema smoke passed"
