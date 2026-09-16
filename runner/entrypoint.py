#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from uuid import UUID

SOURCE = Path("/source")
WORKSPACE = Path("/workspace")
MANIFEST = SOURCE / ".git" / "ai-orchestra-workspace.json"
HEX = frozenset("0123456789abcdef")


def canonical_uuid(value: str, field: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"invalid {field}") from exc
    if str(parsed) != value:
        raise RuntimeError(f"invalid {field}")
    return value


def digest(value: str, field: str, lengths: set[int]) -> str:
    if len(value) not in lengths or any(ch not in HEX for ch in value):
        raise RuntimeError(f"invalid {field}")
    return value


def expected_env() -> dict[str, str]:
    values = {
        "workspace_id": os.environ.get("AI_ORCHESTRA_RUNNER_WORKSPACE_ID", ""),
        "execution_id": os.environ.get("AI_ORCHESTRA_RUNNER_EXECUTION_ID", ""),
        "base_commit": os.environ.get("AI_ORCHESTRA_RUNNER_BASE_COMMIT", ""),
        "preflight_digest": os.environ.get("AI_ORCHESTRA_RUNNER_PREFLIGHT_DIGEST", ""),
    }
    canonical_uuid(values["workspace_id"], "workspace_id")
    canonical_uuid(values["execution_id"], "execution_id")
    digest(values["base_commit"], "base_commit", {40, 64})
    digest(values["preflight_digest"], "preflight_digest", {64})
    return values


def read_manifest() -> dict:
    if MANIFEST.is_symlink() or not MANIFEST.is_file():
        raise RuntimeError("workspace manifest missing")
    if MANIFEST.stat().st_size > 65536:
        raise RuntimeError("workspace manifest too large")
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("workspace manifest invalid")
    return payload


def verify_manifest(payload: dict, expected: dict[str, str]) -> None:
    for field, expected_value in expected.items():
        if payload.get(field) != expected_value:
            raise RuntimeError(f"workspace manifest mismatch: {field}")
    expected_path = f"/workspace/worktrees/managed/{expected['workspace_id']}"
    if payload.get("workspace_path") != expected_path:
        raise RuntimeError("workspace manifest mismatch: workspace_path")


def copy_source() -> None:
    if SOURCE.is_symlink() or not SOURCE.is_dir():
        raise RuntimeError("runner source workspace unavailable")
    if WORKSPACE.is_symlink() or not WORKSPACE.is_dir():
        raise RuntimeError("runner disposable workspace unavailable")
    if any(WORKSPACE.iterdir()):
        raise RuntimeError("runner disposable workspace is not empty")
    shutil.copytree(
        SOURCE,
        WORKSPACE,
        dirs_exist_ok=True,
        symlinks=True,
        copy_function=shutil.copy2,
    )


def main() -> int:
    expected = expected_env()
    payload = read_manifest()
    verify_manifest(payload, expected)
    copy_source()
    os.chdir(WORKSPACE)
    argv = os.sys.argv[1:]
    if not argv:
        raise RuntimeError("runner command missing")
    os.execvp(argv[0], argv)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
