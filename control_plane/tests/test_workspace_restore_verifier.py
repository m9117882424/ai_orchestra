from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from scripts.workspace_restore_verifier import (
    RestoredWorkspaceError,
    verify_restored_git_workspace,
)


def _git(workspace: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _workspace(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    workspace = tmp_path / str(uuid4())
    workspace.mkdir()
    _git(workspace, "init", "-b", "ai/task")
    _git(workspace, "config", "user.name", "Restore Test")
    _git(workspace, "config", "user.email", "restore@example.invalid")
    (workspace / "README.md").write_text("restorable\n", encoding="utf-8")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-m", "fixture")
    commit = _git(workspace, "rev-parse", "HEAD^{commit}")
    tree = _git(workspace, "rev-parse", "HEAD^{tree}")
    manifest = {
        "base_commit": commit,
        "initial_tree": tree,
        "branch_name": "ai/task",
        "tracked_entries": 1,
    }
    return workspace, manifest


def test_restored_ready_workspace_requires_operational_clean_git(tmp_path):
    workspace, manifest = _workspace(tmp_path)

    verify_restored_git_workspace(workspace, manifest, status="ready")

    (workspace / "README.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(RestoredWorkspaceError, match="ready workspace"):
        verify_restored_git_workspace(workspace, manifest, status="ready")


def test_restored_retained_workspace_may_have_a_detached_head(tmp_path):
    workspace, manifest = _workspace(tmp_path)
    _git(workspace, "checkout", "--detach")

    verify_restored_git_workspace(workspace, manifest, status="retained")


def test_restored_workspace_rejects_missing_git_object(tmp_path):
    workspace, manifest = _workspace(tmp_path)
    commit = str(manifest["base_commit"])
    object_path = workspace / ".git" / "objects" / commit[:2] / commit[2:]
    assert object_path.is_file()
    object_path.unlink()

    with pytest.raises(RestoredWorkspaceError, match="integrity check failed"):
        verify_restored_git_workspace(workspace, manifest, status="retained")


def test_restored_workspace_rejects_corrupt_index(tmp_path):
    workspace, manifest = _workspace(tmp_path)
    (workspace / ".git" / "index").write_bytes(
        hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).digest()
    )

    with pytest.raises(RestoredWorkspaceError, match="integrity check failed"):
        verify_restored_git_workspace(workspace, manifest, status="retained")
