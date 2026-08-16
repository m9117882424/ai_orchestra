#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo "Использование: $0 <проект> <задача-slug> --yes" >&2
  echo "Удаляется только чистый зарегистрированный worktree; ветка сохраняется." >&2
}

if (( $# != 3 )) || [[ "$3" != "--yes" ]]; then
  usage
  exit 2
fi

project="$1"
task_slug="$2"
if [[ ! "$project" =~ ^[A-Za-z0-9._-]+$ || "$project" == "." || "$project" == ".." ]]; then
  echo "[FAIL] Некорректное имя проекта" >&2
  exit 2
fi
if [[ ! "$task_slug" =~ ^[a-z0-9][a-z0-9._-]{1,62}$ || "$task_slug" == *".."* ]]; then
  echo "[FAIL] Некорректный slug задачи" >&2
  exit 2
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_dir="$project_root/repos/$project"
target="$project_root/worktrees/$project/$task_slug"
container_repo="/workspace/repos/projects/$project"
container_target="/workspace/worktrees/$project/$task_slug"

if [[ ! -d "$repo_dir/.git" && ! -f "$repo_dir/.git" ]]; then
  echo "[FAIL] $repo_dir не является Git-репозиторием" >&2
  exit 1
fi
if [[ ! -d "$target" ]]; then
  echo "[FAIL] Worktree не найден: $target" >&2
  exit 1
fi
if ! docker compose ps --status running --services | grep -qx opencode; then
  echo "[FAIL] OpenCode не запущен; выполните make up" >&2
  exit 1
fi
if ! docker compose exec -T opencode git -C "$container_repo" worktree list --porcelain \
  | grep -Fxq "worktree $container_target"; then
  echo "[FAIL] Путь не зарегистрирован как worktree этого проекта" >&2
  exit 1
fi
if [[ -n "$(docker compose exec -T opencode git -C "$container_target" status --porcelain --untracked-files=all)" ]]; then
  echo "[FAIL] Worktree содержит незакоммиченные изменения; удаление отменено" >&2
  exit 1
fi

branch="$(docker compose exec -T opencode git -C "$container_target" branch --show-current | tr -d '\r')"
docker compose exec -T opencode git -C "$container_repo" worktree remove "$container_target"
docker compose exec -T opencode git -C "$container_repo" worktree prune

echo "[OK] Чистый worktree удален: $target"
echo "[OK] Ветка сохранена: $branch"
