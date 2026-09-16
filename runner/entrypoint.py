#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

if __package__:
    from .source_snapshot import SourceSnapshotError, source_snapshot_digest
else:
    from source_snapshot import SourceSnapshotError, source_snapshot_digest

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


def expected_snapshot_digest() -> str | None:
    value = os.environ.get("AI_ORCHESTRA_RUNNER_SOURCE_SNAPSHOT_DIGEST")
    if value is None:
        return None
    return digest(value, "source_snapshot_digest", {64})


def verify_git_head(root: Path, expected_base_commit: str) -> None:
    git_dir = root / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise RuntimeError("runner git directory invalid")
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=root, env=env, check=True, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("runner git head unavailable") from exc
    if result.stdout.strip().lower() != expected_base_commit:
        raise RuntimeError("runner git head mismatch")


def verify_source_snapshot(root: Path, expected_digest: str | None) -> None:
    if expected_digest is None:
        return
    try:
        actual = source_snapshot_digest(root)
    except SourceSnapshotError as exc:
        raise RuntimeError(str(exc)) from exc
    if actual != expected_digest:
        raise RuntimeError("runner source snapshot mismatch")


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
    snapshot_digest = expected_snapshot_digest()
    payload = read_manifest()
    verify_manifest(payload, expected)
    verify_git_head(SOURCE, expected["base_commit"])
    verify_source_snapshot(SOURCE, snapshot_digest)
    copy_source()
    verify_git_head(WORKSPACE, expected["base_commit"])
    verify_source_snapshot(WORKSPACE, snapshot_digest)
    os.chdir(WORKSPACE)
    argv = os.sys.argv[1:]
    if not argv:
        raise RuntimeError("runner command missing")
    os.execvp(argv[0], argv)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
