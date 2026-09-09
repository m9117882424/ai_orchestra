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

schema_cli() {
  docker compose run --rm -T --no-deps \
    -e CONTROL_PLANE_ENVIRONMENT=production \
    -e CONTROL_PLANE_SERVER_PASSWORD=ci-only-manager-password-000000 \
    -e CONTROL_PLANE_OPENCODE_PASSWORD=ci-only-opencode-password-000000 \
    control-plane python -m app.schema_cli "$@"
}

echo "[INFO] PostgreSQL legacy-adoption smoke"
docker compose up -d postgres >/dev/null
wait_postgres

docker compose run --rm -T --no-deps \
  -e CONTROL_PLANE_ENVIRONMENT=test \
  control-plane python - <<'PY'
from app.database_base import Base
from app.database_engine import create_configured_engine
import app.models  # noqa: F401

engine = create_configured_engine()
Base.metadata.create_all(engine)
print("[OK] Legacy unversioned schema created")
PY

schema_cli migrate
schema_cli check

echo "[INFO] PostgreSQL Repository Registry constraint smoke"
if docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c "INSERT INTO repositories (
        id, name, remote_url, remote_identity, remote_host, provider,
        enabled, status, execution_profile, assurance_tier, version
      ) VALUES (
        'invalid-registry-row', 'invalid-registry-row',
        'https://github.com/example/invalid.git',
        'github.com/example/invalid', 'github.com', 'github',
        TRUE, 'trusted_without_validation', 'development',
        'general-standard', 1
      );" >/dev/null 2>&1; then
  echo "[FAIL] PostgreSQL accepted forged Repository Registry status" >&2
  exit 1
fi

invalid_rows="$(docker compose exec -T postgres \
  psql -U ai_orchestra -d ai_orchestra -Atc \
  "SELECT count(*) FROM repositories WHERE id = 'invalid-registry-row'")"
if [[ "$invalid_rows" != "0" ]]; then
  echo "[FAIL] Rejected Repository Registry row was not rolled back" >&2
  exit 1
fi

if docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c "INSERT INTO repositories (
        id, name, remote_url, remote_identity, remote_host, provider,
        enabled, status, execution_profile, assurance_tier, version,
        sync_generation, sync_failure_count
      ) VALUES (
        'invalid-ready-row', 'invalid-ready-row',
        'https://github.com/example/invalid-ready.git',
        'github.com/example/invalid-ready', 'github.com', 'github',
        TRUE, 'ready', 'development', 'general-standard', 1, 0, 0
      );" >/dev/null 2>&1; then
  echo "[FAIL] PostgreSQL accepted ready repository without trusted sync evidence" >&2
  exit 1
fi

echo "[INFO] PostgreSQL Repo Manager lease-pair constraint smoke"
if docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c "INSERT INTO repositories (
        id, name, remote_url, remote_identity, remote_host, provider,
        enabled, status, execution_profile, assurance_tier, version,
        sync_generation, sync_failure_count, sync_lease_owner
      ) VALUES (
        'invalid-lease-row', 'invalid-lease-row',
        'https://github.com/example/invalid-lease.git',
        'github.com/example/invalid-lease', 'github.com', 'github',
        TRUE, 'validating', 'development', 'general-standard', 1,
        1, 0, 'worker-without-expiry'
      );" >/dev/null 2>&1; then
  echo "[FAIL] PostgreSQL accepted half-populated Repo Manager lease" >&2
  exit 1
fi

echo "[INFO] PostgreSQL Repo Manager validating-state constraint smoke"
if docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c "INSERT INTO repositories (
        id, name, remote_url, remote_identity, remote_host, provider,
        enabled, status, execution_profile, assurance_tier, version,
        sync_generation, sync_failure_count, sync_lease_owner,
        sync_lease_expires_at
      ) VALUES (
        'invalid-validating-row', 'invalid-validating-row',
        'https://github.com/example/invalid-validating.git',
        'github.com/example/invalid-validating', 'github.com', 'github',
        TRUE, 'validating', 'development', 'general-standard', 1,
        1, 0, 'worker', CURRENT_TIMESTAMP + INTERVAL '5 minutes'
      );" >/dev/null 2>&1; then
  echo "[FAIL] PostgreSQL accepted validating repository without start evidence" >&2
  exit 1
fi

echo "[INFO] PostgreSQL drift refusal smoke"
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c 'DROP INDEX ix_execution_runs_task_id;' >/dev/null
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c 'ALTER TABLE repositories DROP CONSTRAINT ck_repositories_status;' >/dev/null
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c 'ALTER TABLE repositories DROP CONSTRAINT ck_repositories_sync_lease_pair;' >/dev/null
docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U ai_orchestra -d ai_orchestra \
  -c 'ALTER TABLE repositories DROP CONSTRAINT ck_repositories_validating_state;' >/dev/null

if schema_cli check; then
  echo "[FAIL] schema check accepted deliberately removed index/constraint" >&2
  exit 1
fi

echo "[INFO] PostgreSQL fresh-migration smoke"
docker compose down -v --remove-orphans >/dev/null
docker compose up -d postgres >/dev/null
wait_postgres

schema_cli migrate
schema_cli check

echo "[OK] PostgreSQL schema smoke passed"
