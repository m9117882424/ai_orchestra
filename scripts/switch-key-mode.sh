#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

mode="${1:-}"
restart="${2:---restart}"

case "$mode" in
  shared)
    source_config="config/opencode.shared.json"
    ;;
  separate)
    source_config="config/opencode.separate.json"
    ;;
  *)
    echo "Использование: $0 shared|separate [--restart|--no-restart]" >&2
    exit 2
    ;;
esac

python3 -m json.tool "$source_config" >/dev/null
mkdir -p runtime
temporary_config="runtime/opencode.json.tmp"
cp "$source_config" "$temporary_config"
mv "$temporary_config" runtime/opencode.json

if [[ -f .env ]]; then
  if grep -q '^KEY_MODE=' .env; then
    sed -i "s/^KEY_MODE=.*/KEY_MODE=${mode}/" .env
  else
    printf '\nKEY_MODE=%s\n' "$mode" >> .env
  fi
fi

echo "Режим ключей переключен: $mode"

if [[ "$restart" == "--no-restart" ]]; then
  exit 0
fi

if [[ "$restart" != "--restart" ]]; then
  echo "Неизвестный аргумент: $restart" >&2
  exit 2
fi

if command -v docker >/dev/null 2>&1 && docker compose ps -q opencode 2>/dev/null | grep -q .; then
  docker compose up -d --force-recreate opencode
  echo "Контейнер пересоздан с новой маршрутизацией."
else
  echo "Контейнер не запущен; новый режим применится при следующем make up."
fi

