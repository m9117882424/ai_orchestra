#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

if [[ ! -f .env ]]; then
  echo "[FAIL] .env не найден; выполните make init" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

retention_days="${BACKUP_RETENTION_DAYS:-14}"
if [[ ! "$retention_days" =~ ^[0-9]+$ ]]; then
  echo "[FAIL] BACKUP_RETENTION_DAYS должен быть целым неотрицательным числом" >&2
  exit 1
fi

backup_root="$project_root/backups"
mkdir -p "$backup_root"
timestamp="$(date -u +'%Y%m%dT%H%M%SZ')"
archive="$backup_root/ai-orchestra-$timestamp.tar.gz"
staging_dir="$(mktemp -d /tmp/ai-orchestra-backup.XXXXXX)"
checksum_tmp="$(mktemp /tmp/ai-orchestra-checksums.XXXXXX)"

cleanup() {
  find "$staging_dir" -depth -delete 2>/dev/null || true
  rm -f "$checksum_tmp"
}
trap cleanup EXIT

if ! docker compose ps --status running --services | grep -qx postgres; then
  echo "[FAIL] PostgreSQL кабинета не запущен; резервная копия не будет создана" >&2
  exit 1
fi

mkdir -p "$staging_dir/configuration" "$staging_dir/git-bundles"

docker compose exec -T postgres \
  pg_dump -U ai_orchestra -d ai_orchestra --format=custom \
  > "$staging_dir/control-plane.pgdump"

cp -R \
  .github \
  config \
  control_plane \
  deploy \
  model_gateway \
  model_router \
  prompts \
  policy \
  scripts \
  .env.example \
  .env.providers.example \
  Dockerfile \
  docker-compose.yml \
  Makefile \
  README.md \
  "$staging_dir/configuration/"

if [[ -d data/opencode || -d data/state ]]; then
  tar -czf "$staging_dir/opencode-state.tar.gz" \
    --exclude='data/opencode/auth.json' \
    --exclude='data/opencode/**/auth.json' \
    -C "$project_root" \
    data/opencode data/state
fi

while IFS= read -r -d '' git_dir; do
  repo_dir="$(dirname "$git_dir")"
  repo_name="$(basename "$repo_dir")"
  if [[ "$repo_name" =~ ^[A-Za-z0-9._-]+$ ]]; then
    git -C "$repo_dir" bundle create "$staging_dir/git-bundles/$repo_name.bundle" --all
  else
    echo "[WARN] Пропущен репозиторий с небезопасным именем: $repo_name" >&2
  fi
done < <(find "$project_root/repos" -mindepth 2 -maxdepth 2 -type d -name .git -print0)

(
  cd "$staging_dir"
  find . -type f -print0 \
    | sort -z \
    | xargs -0 sha256sum > "$checksum_tmp"
)
mv "$checksum_tmp" "$staging_dir/SHA256SUMS"

tar -czf "$archive.tmp" -C "$staging_dir" .
chmod 600 "$archive.tmp"
mv "$archive.tmp" "$archive"

deleted_count=0
while IFS= read -r -d '' expired; do
  find "$expired" -maxdepth 0 -type f -delete
  deleted_count=$((deleted_count + 1))
done < <(find "$backup_root" -maxdepth 1 -type f -name 'ai-orchestra-*.tar.gz' -mtime "+$retention_days" -print0)

echo "[OK] Резервная копия: $archive"
echo "[OK] .env, .env.providers и OpenCode auth.json намеренно не включены; храните секреты отдельно"
echo "[OK] Удалено устаревших архивов: $deleted_count"
