#!/bin/sh
set -eu

target=/workspace/worktrees/managed

fail() {
  echo "[FAIL] Task workspace volume initialization failed: $1" >&2
  exit 1
}

[ "$(id -u)" = 0 ] || fail "must run as root"
[ -d "$target" ] || fail "target directory is missing"
[ ! -L "$target" ] || fail "target must not be a symlink"
chown 10001:10001 "$target" || fail "cannot set owner"
chmod 0700 "$target" || fail "cannot set mode"
actual="$(stat -c '%u:%g:%a' "$target")" || fail "cannot inspect owner/mode"
[ "$actual" = "10001:10001:700" ] || fail "unexpected owner/mode $actual"

echo "[OK] Task workspace volume owner/mode=$actual"
