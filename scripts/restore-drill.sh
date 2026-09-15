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

start_epoch="$(date +%s)"
start_utc="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
archive_mtime="$(stat -c '%Y' "$archive")"
archive_sha="$(sha256sum "$archive" | awk '{print $1}')"
archive_bytes="$(stat -c '%s' "$archive")"
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

working_archive="$staging_dir/source-backup.tar.gz"
cp --reflink=auto -- "$archive" "$working_archive"
chmod 600 "$working_archive"
working_sha="$(sha256sum "$working_archive" | awk '{print $1}')"
if [[ "$working_sha" != "$archive_sha" \
  || "$(sha256sum "$archive" | awk '{print $1}')" != "$archive_sha" \
  || "$(stat -c '%s' "$archive")" != "$archive_bytes" ]]; then
  echo "[FAIL] Source backup changed while creating the restore snapshot" >&2
  exit 1
fi

bash ./scripts/verify-backup.sh "$working_archive"

fail_with_db_logs() {
  echo "[FAIL] $1" >&2
  docker logs "$db_container" >&2 || true
  docker inspect "$db_container" --format 'state={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} error={{.State.Error}}' >&2 || true
  exit 1
}

mkdir -p "$staging_dir/payload"
tar -xzf "$working_archive" -C "$staging_dir/payload"
dump="$staging_dir/payload/control-plane.pgdump"
backup_format=1
if [[ -f "$staging_dir/payload/BACKUP_FORMAT" ]]; then
  backup_format="$(tr -d '\r\n' < "$staging_dir/payload/BACKUP_FORMAT")"
fi
workspace_restore_root="$staging_dir/task-workspaces"
mkdir -p "$workspace_restore_root"
if [[ "$backup_format" == "2" ]]; then
  tar -xzf "$staging_dir/payload/task-workspaces.tar.gz" \
    -C "$workspace_restore_root" \
    --no-same-owner --no-same-permissions --delay-directory-restore
fi

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

# The official postgres image first starts a temporary init server. pg_isready can
# succeed against that server before POSTGRES_DB has been created, then the entrypoint
# shuts it down and starts the final server. Do not restore until initialization has
# explicitly completed and the final target database accepts a real query.
init_complete=0
for _ in $(seq 1 90); do
  if [[ "$(docker inspect "$db_container" --format '{{.State.Running}}' 2>/dev/null || true)" != "true" ]]; then
    fail_with_db_logs "Restore-drill PostgreSQL stopped during initialization"
  fi
  if docker logs "$db_container" 2>&1 | grep -Fq 'PostgreSQL init process complete; ready for start up.'; then
    init_complete=1
    break
  fi
  sleep 1
done
if [[ "$init_complete" != "1" ]]; then
  fail_with_db_logs "Restore-drill PostgreSQL initialization did not complete"
fi

ready=0
for _ in $(seq 1 60); do
  if [[ "$(docker inspect "$db_container" --format '{{.State.Running}}' 2>/dev/null || true)" != "true" ]]; then
    fail_with_db_logs "Restore-drill PostgreSQL stopped before final readiness"
  fi
  if docker exec "$db_container" pg_isready -U "$db_user" -d "$db_name" >/dev/null 2>&1 \
    && [[ "$(docker exec "$db_container" psql -U "$db_user" -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='$db_name'" 2>/dev/null || true)" == "1" ]] \
    && [[ "$(docker exec "$db_container" psql -U "$db_user" -d "$db_name" -Atc 'SELECT 1' 2>/dev/null || true)" == "1" ]]; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != "1" ]]; then
  fail_with_db_logs "Restore-drill PostgreSQL final target database did not become ready"
fi

echo "[INFO] Restoring backup into isolated PostgreSQL container"
if ! docker exec -i "$db_container" \
  pg_restore -U "$db_user" -d "$db_name" --no-owner --no-privileges --exit-on-error < "$dump"; then
  fail_with_db_logs "pg_restore failed"
fi
if [[ "$(docker inspect "$db_container" --format '{{.State.Running}}')" != "true" ]]; then
  fail_with_db_logs "Disposable PostgreSQL stopped after pg_restore"
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

workspace_rows_file="$staging_dir/task-workspaces.tsv"
docker exec "$db_container" psql -U "$db_user" -d "$db_name" -AtF $'\t' \
  -c "SELECT w.id, w.status, w.task_id, w.repository_id, w.base_commit, w.branch_name, w.opencode_path, COALESCE(w.initial_tree, ''), COALESCE(w.preflight_digest, ''), COALESCE(w.tracked_entries::text, ''), COALESCE(e.id, '') FROM task_workspaces AS w LEFT JOIN execution_runs AS e ON e.workspace_id = w.id ORDER BY w.id" \
  > "$workspace_rows_file"
workspace_restore_file="$staging_dir/task-workspace-restore.json"
python3 - \
  "$backup_format" "$workspace_restore_root" "$workspace_rows_file" \
  "$workspace_restore_file" "$project_root" <<'PY'
import hashlib
import json
from pathlib import Path
import re
import stat
import sys

backup_format, root_raw, rows_raw, evidence_raw, project_root = sys.argv[1:]
sys.path.insert(0, project_root)
from scripts.workspace_restore_verifier import verify_restored_git_workspace

root = Path(root_raw)
rows = []
for line in Path(rows_raw).read_text(encoding="utf-8").splitlines():
    fields = line.split("\t")
    if len(fields) != 11:
        raise SystemExit("[FAIL] Invalid task workspace database evidence row")
    (
        workspace_id,
        status,
        task_id,
        repository_id,
        base_commit,
        branch_name,
        opencode_path,
        initial_tree,
        preflight_digest,
        tracked_entries_raw,
        execution_id,
    ) = fields
    try:
        tracked_entries = (
            int(tracked_entries_raw) if tracked_entries_raw else None
        )
    except ValueError as exc:
        raise SystemExit("[FAIL] Invalid tracked_entries database evidence") from exc
    rows.append(
        {
            "id": workspace_id,
            "status": status,
            "task_id": task_id,
            "repository_id": repository_id,
            "base_commit": base_commit,
            "branch_name": branch_name,
            "opencode_path": opencode_path,
            "initial_tree": initial_tree or None,
            "preflight_digest": preflight_digest or None,
            "tracked_entries": tracked_entries,
            "execution_id": execution_id or None,
        }
    )

if backup_format == "1" and rows:
    raise SystemExit(
        "[FAIL] Legacy backup has workspace database rows but no task-workspaces payload"
    )
if backup_format not in {"1", "2"}:
    raise SystemExit(f"[FAIL] Unsupported backup format during restore: {backup_format}")

missing = []
invalid_removed = []
invalid_evidence = []
manifest_failures = []
workspace_manifests_verified = 0
uuid_re = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
always_requires_final = {
    "ready",
    "inspection_pending",
    "inspecting",
    "retained",
}
manifest_keys = {
    "contract_version",
    "execution_id",
    "task_id",
    "repository_id",
    "workspace_id",
    "base_commit",
    "branch_name",
    "workspace_path",
    "initial_tree",
    "tracked_entries",
    "preflight_digest",
}

def verify_manifest(row, final: Path) -> None:
    global workspace_manifests_verified
    manifest = final / ".git" / "ai-orchestra-workspace.json"
    try:
        metadata = manifest.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_size > 65536
        ):
            raise ValueError("invalid metadata")
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("manifest is missing or invalid") from exc
    if not isinstance(payload, dict) or set(payload) != manifest_keys:
        raise ValueError("manifest shape mismatch")
    commit_re = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
    branch = payload.get("branch_name")
    tracked = payload.get("tracked_entries")
    if (
        type(payload.get("contract_version")) is not int
        or payload.get("contract_version") != 1
        or any(
            not isinstance(payload.get(key), str)
            or uuid_re.fullmatch(payload[key]) is None
            for key in ("execution_id", "task_id", "repository_id", "workspace_id")
        )
        or not isinstance(payload.get("base_commit"), str)
        or commit_re.fullmatch(payload["base_commit"]) is None
        or not isinstance(payload.get("initial_tree"), str)
        or commit_re.fullmatch(payload["initial_tree"]) is None
        or not isinstance(branch, str)
        or re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,253}[A-Za-z0-9])?",
            branch,
        ) is None
        or ".." in branch
        or "//" in branch
        or "@{" in branch
        or branch == "@"
        or branch.endswith(("/", ".", ".lock"))
        or any(
            component.startswith(".") or component.endswith(".lock")
            for component in branch.split("/")
        )
        or type(tracked) is not int
        or tracked < 0
        or not isinstance(payload.get("workspace_path"), str)
        or not payload["workspace_path"].startswith("/")
        or "\x00" in payload["workspace_path"]
    ):
        raise ValueError("manifest values are invalid")
    digest = payload.get("preflight_digest")
    unsigned = dict(payload)
    unsigned.pop("preflight_digest", None)
    calculated = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if not isinstance(digest, str) or digest != calculated:
        raise ValueError("manifest digest mismatch")
    expected = {
        "contract_version": 1,
        "execution_id": row["execution_id"],
        "task_id": row["task_id"],
        "repository_id": row["repository_id"],
        "workspace_id": row["id"],
        "base_commit": row["base_commit"],
        "branch_name": row["branch_name"],
        "workspace_path": row["opencode_path"],
    }
    if any(value is None for value in expected.values()):
        raise ValueError("database binding is incomplete")
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("manifest/database binding mismatch")
    evidence = (
        row["initial_tree"],
        row["preflight_digest"],
        row["tracked_entries"],
    )
    if any(value is not None for value in evidence):
        if any(value is None for value in evidence):
            raise ValueError("database preflight evidence is incomplete")
        if (
            payload.get("initial_tree") != row["initial_tree"]
            or payload.get("preflight_digest") != row["preflight_digest"]
            or payload.get("tracked_entries") != row["tracked_entries"]
        ):
            raise ValueError("manifest/database preflight evidence mismatch")
    verify_restored_git_workspace(final, payload, status=row["status"])
    workspace_manifests_verified += 1

required_workspace_directories_verified = 0
known_workspace_ids = {row["id"] for row in rows}
orphan_directories = sorted(
    candidate.name
    for candidate in root.iterdir()
    if uuid_re.fullmatch(candidate.name)
    and candidate.name not in known_workspace_ids
)
for row in rows:
    workspace_id = row["id"]
    status = row["status"]
    final = root / workspace_id
    evidence = (
        row["initial_tree"],
        row["preflight_digest"],
        row["tracked_entries"],
    )
    evidence_complete = all(value is not None for value in evidence)
    if (
        status not in {"invalid", "removed"}
        and any(value is not None for value in evidence)
        and not evidence_complete
    ):
        invalid_evidence.append(workspace_id)
    if status in always_requires_final and not evidence_complete:
        invalid_evidence.append(workspace_id)
    requires_final = status in always_requires_final or (
        status == "cleanup_pending" and evidence_complete
    )
    if requires_final and not final.is_dir():
        missing.append(f"{workspace_id}:{status}")
    elif requires_final:
        required_workspace_directories_verified += 1
    if status == "removed" and (
        final.exists()
        or final.is_symlink()
        or any(root.glob(f".{workspace_id}.*.prepare"))
        or any(root.glob(f".{workspace_id}.*.remove"))
    ):
        invalid_removed.append(workspace_id)
    if final.is_dir() and status not in {"invalid", "removed"}:
        try:
            verify_manifest(row, final)
        except ValueError as exc:
            manifest_failures.append(f"{workspace_id}:{exc}")

if missing:
    raise SystemExit(f"[FAIL] Required restored workspace directories missing: {missing}")
if invalid_removed:
    raise SystemExit(f"[FAIL] Removed workspaces unexpectedly restored: {invalid_removed}")
if invalid_evidence:
    raise SystemExit(f"[FAIL] Incomplete workspace evidence in database: {invalid_evidence}")
if manifest_failures:
    raise SystemExit(f"[FAIL] Restored workspace manifests invalid: {manifest_failures}")
if orphan_directories:
    raise SystemExit(
        f"[FAIL] Restored workspace directories missing database rows: {orphan_directories}"
    )

payload = {
    "backup_format": int(backup_format),
    "database_rows": len(rows),
    "required_workspace_directories_verified": required_workspace_directories_verified,
    "workspace_manifests_verified": workspace_manifests_verified,
    "cleaning_rows_verified": len(
        [row for row in rows if row["status"] == "cleaning"]
    ),
}
Path(evidence_raw).write_text(
    json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(
    "[OK] Task workspace restore reconciled: "
    f"format={backup_format}, database_rows={len(rows)}"
)
PY

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
  "$table_counts_file" "$workspace_restore_file" <<'PY'
import json
import sys
(
    evidence, archive, archive_sha, started, finished,
    backup_age, restore_seconds, pre_revision, post_revision,
    git_sha, postgres_image, postgres_image_id, control_image, control_image_id,
    counts_path, workspace_restore_path,
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
    "task_workspace_restore": json.load(open(workspace_restore_path, encoding="utf-8")),
    "scope_note": "Observed values are drill evidence, not contractual RPO/RTO targets.",
}
with open(evidence, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
    fh.write("\n")
PY
chmod 600 "$evidence"

final_archive_sha="$(sha256sum "$archive" | awk '{print $1}')"
final_archive_bytes="$(stat -c '%s' "$archive")"
if [[ "$final_archive_sha" != "$archive_sha" \
  || "$final_archive_bytes" != "$archive_bytes" ]]; then
  echo "[FAIL] Source backup changed while restore drill was running" >&2
  exit 1
fi

printf '[OK] Clean restore drill succeeded: revision=%s, restore=%ss, backup_age=%ss\n' \
  "$post_revision" "$restore_seconds" "$backup_age_seconds"
echo "[OK] Evidence: $evidence"
