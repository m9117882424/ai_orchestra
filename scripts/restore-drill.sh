#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

archive="${1:-}"
if [[ -z "$archive" ]]; then
  archive="$(find "$project_root/backups" -maxdepth 1 -type f -name 'ai-orchestra-*.tar.gz' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | cut -d' ' -f2-)"
fi
if [[ -z "$archive" ]]; then
  echo "[FAIL] No local backup found. Pass an archive path or run: make backup" >&2
  exit 1
fi
archive="$(realpath "$archive")"

bash ./scripts/verify-backup.sh "$archive"

start_epoch="$(date +%s)"
start_utc="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
archive_mtime="$(stat -c '%Y' "$archive")"
archive_sha="$(sha256sum "$archive" | awk '{print $1}')"
backup_age_seconds=$((start_epoch - archive_mtime))
if (( backup_age_seconds < 0 )); then backup_age_seconds=0; fi

staging_dir="$(mktemp -d /tmp/ai-orchestra-restore-drill.XXXXXX)"
run_id="$(date -u +'%Y%m%dT%H%M%SZ')-$$"
db_container="ai-orchestra-restore-$run_id"
network_name="ai-orchestra-restore-net-$run_id"
volume_name="ai-orchestra-restore-vol-$run_id"
db_name="ai_orchestra_restore"
db_user="ai_orchestra_restore"
db_password="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(24))
PY
)"

cleanup() {
  docker rm -f "$db_container" >/dev/null 2>&1 || true
  docker volume rm "$volume_name" >/dev/null 2>&1 || true
  docker network rm "$network_name" >/dev/null 2>&1 || true
  find "$staging_dir" -depth -delete 2>/dev/null || true
}
trap cleanup EXIT

mkdir -p "$staging_dir/payload"
tar -xzf "$archive" -C "$staging_dir/payload"
dump="$staging_dir/payload/control-plane.pgdump"

postgres_image="$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["postgres"]["image"])')"
control_image="$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["control-plane"]["image"])')"

for image in "$postgres_image" "$control_image"; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "[FAIL] Required local image missing: $image" >&2
    echo "       Build/pull the validated production images before running the drill." >&2
    exit 1
  fi
done
postgres_image_id="$(docker image inspect "$postgres_image" --format '{{.Id}}')"
control_image_id="$(docker image inspect "$control_image" --format '{{.Id}}')"

docker run --rm -i "$postgres_image" pg_restore --list < "$dump" >/dev/null

docker network create "$network_name" >/dev/null
docker volume create "$volume_name" >/dev/null

docker run -d \
  --name "$db_container" \
  --network "$network_name" \
  --network-alias restore-postgres \
  -e "POSTGRES_DB=$db_name" \
  -e "POSTGRES_USER=$db_user" \
  -e "POSTGRES_PASSWORD=$db_password" \
  -v "$volume_name:/var/lib/postgresql/data" \
  "$postgres_image" >/dev/null

ready=0
for _ in $(seq 1 60); do
  if docker exec "$db_container" pg_isready -U "$db_user" -d "$db_name" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != "1" ]]; then
  echo "[FAIL] Restore-drill PostgreSQL did not become ready" >&2
  docker logs "$db_container" >&2 || true
  exit 1
fi

echo "[INFO] Restoring backup into isolated PostgreSQL container"
if ! docker exec -i "$db_container" \
  pg_restore -U "$db_user" -d "$db_name" --no-owner --no-privileges --exit-on-error < "$dump"; then
  echo "[FAIL] pg_restore failed; disposable PostgreSQL logs follow" >&2
  docker logs "$db_container" >&2 || true
  docker inspect "$db_container" --format 'state={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} error={{.State.Error}}' >&2 || true
  exit 1
fi
if [[ "$(docker inspect "$db_container" --format '{{.State.Running}}')" != "true" ]]; then
  echo "[FAIL] Disposable PostgreSQL stopped after pg_restore" >&2
  docker logs "$db_container" >&2 || true
  docker inspect "$db_container" --format 'state={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} error={{.State.Error}}' >&2 || true
  exit 1
fi

has_alembic="$(docker exec "$db_container" psql -U "$db_user" -d "$db_name" -Atc "SELECT to_regclass('public.alembic_version') IS NOT NULL")"
if [[ "$has_alembic" == "t" ]]; then
  pre_revision="$(docker exec "$db_container" psql -U "$db_user" -d "$db_name" -Atc "SELECT COALESCE((SELECT version_num FROM alembic_version LIMIT 1),'empty')")"
else
  pre_revision="unversioned"
fi

connection_url="postgresql+psycopg://${db_user}:${db_password}@restore-postgres:5432/${db_name}"

echo "[INFO] Running current migration logic only against the restored copy"
docker run --rm --network "$network_name" \
  -e CONTROL_PLANE_ENVIRONMENT=test \
  -e "CONTROL_PLANE_DATABASE_URL=$connection_url" \
  "$control_image" python -m app.schema_cli migrate
docker run --rm --network "$network_name" \
  -e CONTROL_PLANE_ENVIRONMENT=test \
  -e "CONTROL_PLANE_DATABASE_URL=$connection_url" \
  "$control_image" python -m app.schema_cli check

post_revision="$(docker exec "$db_container" psql -U "$db_user" -d "$db_name" -Atc "SELECT version_num FROM alembic_version LIMIT 1")"

table_counts_file="$staging_dir/table-counts.tsv"
: > "$table_counts_file"
while IFS= read -r table_name; do
  [[ -z "$table_name" ]] && continue
  if [[ ! "$table_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "[FAIL] Unexpected table name in restored database: $table_name" >&2
    exit 1
  fi
  count="$(docker exec "$db_container" psql -U "$db_user" -d "$db_name" -Atc "SELECT count(*) FROM \"$table_name\"")"
  printf '%s\t%s\n' "$table_name" "$count" >> "$table_counts_file"
done < <(docker exec "$db_container" psql -U "$db_user" -d "$db_name" -Atc "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")

end_epoch="$(date +%s)"
end_utc="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
restore_seconds=$((end_epoch - start_epoch))

evidence_dir="$project_root/backups/drills"
mkdir -p "$evidence_dir"
evidence="$evidence_dir/restore-drill-$run_id.json"

git_sha="$(git rev-parse HEAD 2>/dev/null || printf unknown)"
python3 - \
  "$evidence" "$archive" "$archive_sha" "$start_utc" "$end_utc" \
  "$backup_age_seconds" "$restore_seconds" "$pre_revision" "$post_revision" \
  "$git_sha" "$postgres_image" "$postgres_image_id" "$control_image" "$control_image_id" \
  "$table_counts_file" <<'PY'
import json
import sys
(
    evidence, archive, archive_sha, started, finished,
    backup_age, restore_seconds, pre_revision, post_revision,
    git_sha, postgres_image, postgres_image_id, control_image, control_image_id,
    counts_path,
) = sys.argv[1:]
counts = {}
with open(counts_path, encoding="utf-8") as fh:
    for line in fh:
        table, count = line.rstrip("\n").split("\t", 1)
        counts[table] = int(count)
payload = {
    "result": "success",
    "source_backup": archive,
    "source_backup_sha256": archive_sha,
    "started_utc": started,
    "finished_utc": finished,
    "observed_backup_age_seconds": int(backup_age),
    "observed_restore_rto_seconds": int(restore_seconds),
    "pre_migration_revision": pre_revision,
    "post_migration_revision": post_revision,
    "orchestra_git_sha": git_sha,
    "postgres_image": postgres_image,
    "postgres_image_id": postgres_image_id,
    "control_plane_image": control_image,
    "control_plane_image_id": control_image_id,
    "restored_table_counts": counts,
    "scope_note": "Observed values are drill evidence, not contractual RPO/RTO targets.",
}
with open(evidence, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
    fh.write("\n")
PY
chmod 600 "$evidence"

printf '[OK] Clean restore drill succeeded: revision=%s, restore=%ss, backup_age=%ss\n' \
  "$post_revision" "$restore_seconds" "$backup_age_seconds"
echo "[OK] Evidence: $evidence"
