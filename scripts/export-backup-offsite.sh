#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

archive="${1:-}"
if [[ -z "$archive" ]]; then
  archive="$(find "$project_root/backups" -maxdepth 1 -type f -name 'ai-orchestra-*.tar.gz' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | cut -d' ' -f2-)"
fi
if [[ -z "$archive" || ! -f "$archive" ]]; then
  echo "[FAIL] Backup archive not found. Pass a path or run: make backup" >&2
  exit 1
fi
archive="$(realpath "$archive")"

bash ./scripts/verify-backup.sh "$archive"

offsite_dir="${BACKUP_OFFSITE_DIR:-}"
if [[ -z "$offsite_dir" ]]; then
  echo "[FAIL] BACKUP_OFFSITE_DIR is not configured" >&2
  exit 1
fi
if [[ "$offsite_dir" != /* ]]; then
  echo "[FAIL] BACKUP_OFFSITE_DIR must be an absolute path" >&2
  exit 1
fi
mkdir -p "$offsite_dir"
if [[ -L "$offsite_dir" ]]; then
  echo "[FAIL] Refusing symlink offsite destination: $offsite_dir" >&2
  exit 1
fi
offsite_dir="$(realpath "$offsite_dir")"

case "$offsite_dir/" in
  "$project_root/"*)
    echo "[FAIL] Offsite destination must not live inside the project tree" >&2
    exit 1
    ;;
esac

if [[ "${BACKUP_OFFSITE_REQUIRE_MOUNTPOINT:-1}" == "1" ]] && ! mountpoint -q "$offsite_dir"; then
  echo "[FAIL] BACKUP_OFFSITE_DIR must be a distinct mountpoint when BACKUP_OFFSITE_REQUIRE_MOUNTPOINT=1" >&2
  exit 1
fi
if [[ "${BACKUP_OFFSITE_ENCRYPTION_AT_REST_CONFIRMED:-no}" != "yes" ]]; then
  echo "[FAIL] Set BACKUP_OFFSITE_ENCRYPTION_AT_REST_CONFIRMED=yes only after the destination provides encryption at rest" >&2
  exit 1
fi
if [[ "${BACKUP_OFFSITE_AUTHENTICATED_TRANSPORT_CONFIRMED:-no}" != "yes" ]]; then
  echo "[FAIL] Set BACKUP_OFFSITE_AUTHENTICATED_TRANSPORT_CONFIRMED=yes only after the mount transport is authenticated/encrypted" >&2
  exit 1
fi

base="$(basename "$archive")"
final="$offsite_dir/$base"
tmp="$offsite_dir/.${base}.partial.$$"
manifest_tmp="$offsite_dir/.${base}.manifest.partial.$$"
manifest="$offsite_dir/${base}.manifest.json"
cleanup() {
  rm -f "$tmp" "$manifest_tmp"
}
trap cleanup EXIT

if [[ -e "$final" || -e "$manifest" ]]; then
  echo "[FAIL] Refusing to overwrite existing offsite backup: $base" >&2
  exit 1
fi

source_sha="$(sha256sum "$archive" | awk '{print $1}')"
source_bytes="$(stat -c '%s' "$archive")"
cp --reflink=never --preserve=mode,timestamps "$archive" "$tmp"
chmod 600 "$tmp"
remote_sha="$(sha256sum "$tmp" | awk '{print $1}')"
if [[ "$remote_sha" != "$source_sha" ]]; then
  echo "[FAIL] Offsite copy SHA-256 mismatch" >&2
  exit 1
fi
mv "$tmp" "$final"

python3 - "$manifest_tmp" "$base" "$source_sha" "$source_bytes" "$(git rev-parse HEAD 2>/dev/null || printf unknown)" <<'PY'
import json
import socket
import sys
from datetime import datetime, timezone

path, backup_name, sha256, size_bytes, git_sha = sys.argv[1:]
payload = {
    "backup_name": backup_name,
    "sha256": sha256,
    "size_bytes": int(size_bytes),
    "exported_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "source_host": socket.gethostname(),
    "orchestra_git_sha": git_sha,
    "encryption_at_rest_confirmed_by_operator": True,
    "authenticated_transport_confirmed_by_operator": True,
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
    fh.write("\n")
PY
chmod 600 "$manifest_tmp"
mv "$manifest_tmp" "$manifest"

# Re-read the committed destination bytes after rename.
final_sha="$(sha256sum "$final" | awk '{print $1}')"
if [[ "$final_sha" != "$source_sha" ]]; then
  echo "[FAIL] Offsite backup changed after atomic rename" >&2
  exit 1
fi

printf '[OK] Offsite backup exported: %s\n' "$final"
printf '[OK] sha256=%s bytes=%s\n' "$final_sha" "$source_bytes"
echo "[OK] Manifest: $manifest"
