"""Fail-closed Git integrity checks for restored task workspaces."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import tempfile


class RestoredWorkspaceError(ValueError):
    """The restored directory cannot satisfy the durable workspace contract."""


def _git(
    workspace: Path,
    arguments: list[str],
    *,
    output_limit: int = 64 * 1024 * 1024,
    timeout_seconds: int = 30,
) -> bytes:
    environment = {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    command = [
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.allow=never",
        "-C",
        str(workspace),
        *arguments,
    ]
    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                command,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                return_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    process.kill()
                process.wait()
                raise RestoredWorkspaceError("git operation timed out") from exc
            if return_code != 0:
                raise RestoredWorkspaceError("git repository integrity check failed")
            size = output.tell()
            if size > output_limit:
                raise RestoredWorkspaceError("git output exceeds restore verification limit")
            output.seek(0)
            return output.read(output_limit + 1)
    except FileNotFoundError as exc:
        raise RestoredWorkspaceError("git executable is unavailable") from exc


def _text(workspace: Path, arguments: list[str]) -> str:
    try:
        return _git(workspace, arguments, output_limit=1024 * 1024).decode(
            "utf-8", errors="strict"
        ).strip()
    except UnicodeError as exc:
        raise RestoredWorkspaceError("git output is not valid UTF-8") from exc


def verify_restored_git_workspace(
    workspace: Path,
    manifest: dict[str, object],
    *,
    status: str,
    max_files: int = 100_000,
) -> None:
    """Prove that Git metadata and the manifest describe a usable workspace."""
    git_directory = workspace / ".git"
    try:
        if git_directory.is_symlink() or not git_directory.is_dir():
            raise RestoredWorkspaceError(".git directory is missing or invalid")
    except OSError as exc:
        raise RestoredWorkspaceError(".git directory is unavailable") from exc

    base_commit = str(manifest["base_commit"])
    initial_tree = str(manifest["initial_tree"])
    base = _text(workspace, ["rev-parse", "--verify", f"{base_commit}^{{commit}}"])
    base_tree = _text(
        workspace,
        ["rev-parse", "--verify", f"{base_commit}^{{tree}}"],
    )
    head = _text(workspace, ["rev-parse", "--verify", "HEAD^{commit}"])
    tree = _text(workspace, ["rev-parse", "--verify", "HEAD^{tree}"])
    _git(workspace, ["fsck", "--strict", "--no-dangling", "HEAD", base_commit])
    tracked = _git(
        workspace,
        ["ls-files", "-z"],
        output_limit=max(1024 * 1024, max_files * 4096),
    )
    tracked_entries = [entry for entry in tracked.split(b"\0") if entry]
    if len(tracked_entries) > max_files:
        raise RestoredWorkspaceError("tracked file count exceeds restore limit")
    changes = _git(
        workspace,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching"],
    )

    if base != base_commit or base_tree != initial_tree:
        raise RestoredWorkspaceError("base commit/tree does not match manifest")
    if status == "ready":
        branch = _text(workspace, ["symbolic-ref", "--short", "HEAD"])
        if (
            head != base_commit
            or tree != initial_tree
            or branch != manifest["branch_name"]
            or changes
            or len(tracked_entries) != manifest["tracked_entries"]
        ):
            raise RestoredWorkspaceError("ready workspace does not match preflight evidence")
