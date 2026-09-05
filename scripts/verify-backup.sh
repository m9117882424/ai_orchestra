#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

archive="${1:-}"
if [[ -z "$archive" ]]; then
  archive="$(find "$project_root/backups" -maxdepth 1 -type f -name 'ai-orchestra-*.tar.gz' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | cut -d' ' -f2-)"
fi

if [[ -z "$archive" || ! -f "$archive" ]]; then
  echo "[FAIL] Backup archive not found. Pass a path or create one with: make backup" >&2
  exit 1
fi
if [[ -L "$archive" ]]; then
  echo "[FAIL] Refusing symlink backup archive: $archive" >&2
  exit 1
fi

archive="$(realpath "$archive")"
case "$archive" in
  *.tar.gz) ;;
  *) echo "[FAIL] Expected .tar.gz backup archive: $archive" >&2; exit 1 ;;
esac

staging_dir="$(mktemp -d /tmp/ai-orchestra-backup-verify.XXXXXX)"
cleanup() {
  find "$staging_dir" -depth -delete 2>/dev/null || true
}
trap cleanup EXIT

echo "[INFO] Verifying archive container: $archive"
tar -tzf "$archive" > "$staging_dir/archive.list"

python3 - "$staging_dir/archive.list" <<'PY'
from pathlib import PurePosixPath
import sys

listing = open(sys.argv[1], encoding="utf-8").read().splitlines()
if not listing:
    raise SystemExit("[FAIL] backup archive is empty")
for raw in listing:
    value = raw[2:] if raw.startswith("./") else raw
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"[FAIL] unsafe archive path: {raw}")
print(f"[OK] archive paths safe: {len(listing)} entries")
PY

tar -xzf "$archive" -C "$staging_dir/extracted" --one-top-level 2>/dev/null || {
  mkdir -p "$staging_dir/extracted"
  tar -xzf "$archive" -C "$staging_dir/extracted"
}

# GNU tar --one-top-level nests content one level; normalize to the directory containing SHA256SUMS.
root="$staging_dir/extracted"
if [[ ! -f "$root/SHA256SUMS" ]]; then
  found="$(find "$root" -mindepth 1 -maxdepth 2 -type f -name SHA256SUMS -print -quit)"
  if [[ -n "$found" ]]; then
    root="$(dirname "$found")"
  fi
fi

for required in \
  SHA256SUMS \
  control-plane.pgdump \
  configuration/docker-compose.yml \
  configuration/Makefile \
  configuration/control_plane/alembic.ini \
  configuration/control_plane/app; do
  if [[ ! -e "$root/$required" ]]; then
    echo "[FAIL] Required backup payload missing: $required" >&2
    exit 1
  fi
done

(
  cd "$root"
  sha256sum --check --strict SHA256SUMS
)

if [[ ! -s "$root/control-plane.pgdump" ]]; then
  echo "[FAIL] control-plane.pgdump is empty" >&2
  exit 1
fi

if find "$root" -type f \( -name '.env' -o -name '.env.providers' -o -name 'auth.json' \) -print -quit | grep -q .; then
  echo "[FAIL] Secret-bearing file detected inside backup" >&2
  exit 1
fi

archive_sha="$(sha256sum "$archive" | awk '{print $1}')"
archive_bytes="$(stat -c '%s' "$archive")"
printf '[OK] Backup verified: sha256=%s bytes=%s\n' "$archive_sha" "$archive_bytes"
