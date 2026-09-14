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

archive_sha_before="$(sha256sum "$archive" | awk '{print $1}')"
archive_bytes_before="$(stat -c '%s' "$archive")"
verified_archive="$staging_dir/source-backup.tar.gz"
cp --reflink=auto -- "$archive" "$verified_archive"
chmod 600 "$verified_archive"
verified_sha="$(sha256sum "$verified_archive" | awk '{print $1}')"
archive_sha_after_copy="$(sha256sum "$archive" | awk '{print $1}')"
archive_bytes_after_copy="$(stat -c '%s' "$archive")"
if ! [[ "$archive_sha_before" == "$verified_sha" \
  && "$archive_sha_before" == "$archive_sha_after_copy" \
  && "$archive_bytes_before" == "$archive_bytes_after_copy" ]]; then
  echo "[FAIL] Backup archive changed while creating a verification snapshot" >&2
  exit 1
fi

echo "[INFO] Verifying archive container: $archive"
python3 - "$verified_archive" "$staging_dir/archive.list" <<'PY'
from pathlib import PurePosixPath
import sys
import tarfile

archive, listing_path = sys.argv[1:]
seen = set()
listing = []
total = 0
with tarfile.open(archive, "r:gz") as bundle:
    for member in bundle:
        raw = member.name
        value = raw
        while value.startswith("./"):
            value = value[2:]
        value = value.rstrip("/")
        path = PurePosixPath(value)
        if value not in {"", "."} and (path.is_absolute() or ".." in path.parts):
            raise SystemExit(f"[FAIL] unsafe archive path: {raw}")
        if value not in {"", "."} and str(path) != value:
            raise SystemExit(f"[FAIL] non-canonical archive path: {raw}")
        canonical = "." if value in {"", "."} else str(path)
        if canonical in seen:
            raise SystemExit(f"[FAIL] duplicate archive path: {raw}")
        seen.add(canonical)
        if not (member.isdir() or member.isfile()):
            raise SystemExit(f"[FAIL] forbidden outer archive entry type: {raw}")
        total += member.size
        if len(seen) > 250_000 or total > 100 * 1024**3:
            raise SystemExit("[FAIL] backup archive exceeds verification limits")
        listing.append(raw)
if not listing:
    raise SystemExit("[FAIL] backup archive is empty")
open(listing_path, "w", encoding="utf-8").write("\n".join(listing) + "\n")
print(f"[OK] archive paths/types safe: {len(listing)} entries")
PY

mkdir -p "$staging_dir/extracted"
tar -xzf "$verified_archive" -C "$staging_dir/extracted" \
  --no-same-owner --no-same-permissions --delay-directory-restore

root="$staging_dir/extracted"

backup_format=1
if [[ -f "$root/BACKUP_FORMAT" ]]; then
  if [[ "$(tr -d '\r\n' < "$root/BACKUP_FORMAT")" != "2" ]]; then
    echo "[FAIL] Unsupported BACKUP_FORMAT" >&2
    exit 1
  fi
  backup_format=2
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

if [[ "$backup_format" == "2" && ! -s "$root/task-workspaces.tar.gz" ]]; then
  echo "[FAIL] Required backup payload missing: task-workspaces.tar.gz" >&2
  exit 1
fi

python3 - "$root" <<'PY'
from pathlib import Path, PurePosixPath
import re
import sys

root = Path(sys.argv[1]).resolve(strict=True)
manifest = root / "SHA256SUMS"
try:
    raw = manifest.read_text(encoding="utf-8")
except (OSError, UnicodeError) as exc:
    raise SystemExit("[FAIL] SHA256SUMS is not valid UTF-8 text") from exc
if len(raw.encode("utf-8")) > 32 * 1024 * 1024:
    raise SystemExit("[FAIL] SHA256SUMS exceeds verification limits")

listed = set()
line_re = re.compile(r"([0-9a-f]{64}) ([ *])(.+)")
for line in raw.splitlines():
    match = line_re.fullmatch(line)
    if match is None:
        raise SystemExit("[FAIL] Invalid SHA256SUMS entry")
    value = match.group(3)
    while value.startswith("./"):
        value = value[2:]
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or str(path) != value
        or value == "SHA256SUMS"
        or value in listed
    ):
        raise SystemExit(f"[FAIL] Unsafe SHA256SUMS path: {match.group(3)}")
    candidate = root.joinpath(*path.parts)
    try:
        if not candidate.is_file() or candidate.is_symlink():
            raise SystemExit(f"[FAIL] SHA256SUMS entry is not a regular file: {value}")
        candidate.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[FAIL] SHA256SUMS path escapes backup: {value}") from exc
    listed.add(value)

actual = {
    path.relative_to(root).as_posix()
    for path in root.rglob("*")
    if path.is_file() and not path.is_symlink() and path != manifest
}
if listed != actual:
    missing = sorted(actual - listed)[:10]
    unexpected = sorted(listed - actual)[:10]
    raise SystemExit(
        "[FAIL] SHA256SUMS inventory mismatch: "
        f"unlisted={missing}, nonexistent={unexpected}"
    )
print(f"[OK] checksum inventory safe: {len(listed)} files")
PY

(
  cd "$root"
  sha256sum --check --strict --quiet SHA256SUMS
)

if [[ ! -s "$root/control-plane.pgdump" ]]; then
  echo "[FAIL] control-plane.pgdump is empty" >&2
  exit 1
fi

if [[ "$backup_format" == "2" ]]; then
  python3 - "$root/task-workspaces.tar.gz" <<'PY'
from pathlib import PurePosixPath
import posixpath
import re
import sys
import tarfile

uuid = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
workspace_re = re.compile(rf"{uuid}")
lock_re = re.compile(rf"\.({uuid})\.lock")
temporary_re = re.compile(rf"\.({uuid})\.[0-9a-f]{{32}}\.(prepare|remove)")
seen = set()
total = 0

def normalized_name(raw: str) -> str:
    while raw.startswith("./"):
        raw = raw[2:]
    return raw.rstrip("/")

with tarfile.open(sys.argv[1], "r:gz") as bundle:
    for member in bundle:
        value = normalized_name(member.name)
        if value in {"", "."}:
            if not member.isdir():
                raise SystemExit("[FAIL] invalid workspace archive root")
            continue
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or str(path) != value
            or value in seen
        ):
            raise SystemExit(f"[FAIL] unsafe or duplicate workspace path: {member.name}")
        seen.add(value)
        total += member.size
        if len(seen) > 1_000_000 or total > 100 * 1024**3:
            raise SystemExit("[FAIL] workspace archive exceeds verification limits")
        if not (member.isdir() or member.isfile() or member.issym()):
            raise SystemExit(f"[FAIL] forbidden workspace entry type: {member.name}")

        top = path.parts[0]
        root_id = None
        if workspace_re.fullmatch(top):
            root_id = top
        elif match := lock_re.fullmatch(top):
            root_id = match.group(1)
            if len(path.parts) != 1 or not member.isfile():
                raise SystemExit(f"[FAIL] invalid workspace lock entry: {member.name}")
        elif match := temporary_re.fullmatch(top):
            root_id = match.group(1)
        else:
            raise SystemExit(f"[FAIL] unexpected workspace root entry: {member.name}")

        if member.issym():
            target = member.linkname
            if not target or target.startswith("/"):
                raise SystemExit(f"[FAIL] unsafe workspace symlink: {member.name}")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(value), target))
            resolved_path = PurePosixPath(resolved)
            if (
                resolved.startswith("../")
                or resolved == ".."
                or not resolved_path.parts
                or resolved_path.parts[0] != top
                or ".git" in path.parts
                or ".git" in resolved_path.parts
                or root_id is None
            ):
                raise SystemExit(f"[FAIL] escaping workspace symlink: {member.name}")

print(f"[OK] task workspace archive safe: {len(seen)} entries")
PY
fi

if find "$root" -type f \( -name '.env' -o -name '.env.providers' -o -name '.env.repositories' -o -name 'auth.json' \) -print -quit | grep -q .; then
  echo "[FAIL] Secret-bearing file detected inside backup" >&2
  exit 1
fi

archive_sha_final="$(sha256sum "$archive" | awk '{print $1}')"
archive_bytes_final="$(stat -c '%s' "$archive")"
if [[ "$archive_sha_final" != "$archive_sha_before" \
  || "$archive_bytes_final" != "$archive_bytes_before" ]]; then
  echo "[FAIL] Backup archive changed during verification" >&2
  exit 1
fi
printf '[OK] Backup verified: format=%s sha256=%s bytes=%s\n' \
  "$backup_format" "$archive_sha_before" "$archive_bytes_before"
