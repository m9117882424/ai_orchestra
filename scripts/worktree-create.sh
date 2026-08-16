#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo "Использование: $0 <проект> <задача-slug> [base-ref]" >&2
  echo "Пример: $0 arvento-kpp-report fix-mileage origin/main" >&2
}

if (( $# < 2 || $# > 3 )); then
  usage
  exit 2
fi

project="$1"
task_slug="$2"
base_ref="${3:-HEAD}"

if [[ ! "$project" =~ ^[A-Za-z0-9._-]+$ || "$project" == "." || "$project" == ".." ]]; then
  echo "[FAIL] Некорректное имя проекта" >&2
  exit 2
fi
if [[ ! "$task_slug" =~ ^[a-z0-9][a-z0-9._-]{1,62}$ || "$task_slug" == *".."* ]]; then
  echo "[FAIL] Slug задачи: 2–63 символа a-z, 0-9, точка, дефис или подчеркивание" >&2
  exit 2
fi
if [[ ! "$base_ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ || "$base_ref" == *".."* ]]; then
  echo "[FAIL] Некорректная базовая ревизия" >&2
  exit 2
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_dir="$project_root/repos/$project"
worktree_parent="$project_root/worktrees/$project"
target="$worktree_parent/$task_slug"
branch="agent/$task_slug"
container_repo="/workspace/repos/projects/$project"
container_target="/workspace/worktrees/$project/$task_slug"

if [[ ! -d "$repo_dir/.git" && ! -f "$repo_dir/.git" ]]; then
  echo "[FAIL] $repo_dir не является Git-репозиторием" >&2
  exit 1
fi
if [[ -e "$target" ]]; then
  echo "[FAIL] Worktree уже существует: $target" >&2
  exit 1
fi
if ! docker compose ps --status running --services | grep -qx opencode; then
  echo "[FAIL] OpenCode не запущен; выполните make up" >&2
  exit 1
fi
if docker compose exec -T opencode git -C "$container_repo" \
  show-ref --verify --quiet "refs/heads/$branch"; then
  echo "[FAIL] Ветка уже существует: $branch" >&2
  exit 1
fi
if ! docker compose exec -T opencode git -C "$container_repo" \
  rev-parse --verify "${base_ref}^{commit}" >/dev/null 2>&1; then
  echo "[FAIL] Базовая ревизия не найдена: $base_ref" >&2
  exit 1
fi

mkdir -p "$worktree_parent"
docker compose exec -T opencode git -C "$container_repo" \
  worktree add -b "$branch" "$container_target" "$base_ref"

echo "[OK] Worktree создан: $target"
echo "[OK] Путь внутри OpenCode: $container_target"
echo "[OK] Ветка: $branch (база: $base_ref)"
echo "Следующий шаг: назначьте этот путь только одному агенту-редактору."
