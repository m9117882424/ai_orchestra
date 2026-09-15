#!/bin/sh
set -eu

limit="${REPO_MANAGER_GIT_MAX_FILE_BLOCKS:-}"
case "$limit" in
  ''|*[!0-9]*) exit 70 ;;
esac
if [ "$limit" -lt 2048 ]; then
  exit 70
fi

# POSIX shell ulimit -f is expressed in 512-byte blocks. This caps each pack
# file before a hostile or accidentally huge remote can consume the whole host.
ulimit -f "$limit"
exec /usr/bin/git "$@"
