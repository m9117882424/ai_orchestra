from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from uuid import UUID, uuid4


WORKSPACE_CONTRACT_VERSION = 1
WORKSPACE_MANIFEST_NAME = "ai-orchestra-workspace.json"
DEFAULT_OPENCODE_WORKSPACE_ROOT = Path("/workspace/worktrees/managed")
COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
BRANCH_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,253}[A-Za-z0-9])?")
MANIFEST_KEYS = frozenset(
    {
        "contract_version",
        "execution_id",
        "task_id",
        "repository_id",
        "workspace_id",
        "base_commit",
        "branch_name",
        "workspace_path",
        "initial_tree",
        "tracked_entries",
        "preflight_digest",
    }
)


class WorkspacePreflightError(RuntimeError):
    """A classified workspace failure safe to persist or expose to operators."""

    def __init__(self, code: str):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            raise ValueError("Unsafe workspace preflight error code")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WorkspaceBinding:
    execution_id: str
    task_id: str
    repository_id: str
    workspace_id: str
    base_commit: str
    branch_name: str
    workspace_path: str
    initial_tree: str
    tracked_entries: int
    preflight_digest: str


def canonical_uuid(value: str, *, field: str) -> str:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise WorkspacePreflightError(f"{field}_invalid") from exc
    canonical = str(parsed)
    if canonical != value:
        raise WorkspacePreflightError(f"{field}_invalid")
    return canonical


def normalize_commit(value: str, *, field: str = "base_commit") -> str:
    candidate = str(value).lower()
    if not COMMIT_RE.fullmatch(candidate):
        raise WorkspacePreflightError(f"{field}_invalid")
    return candidate


def validate_branch(value: str) -> str:
    if (
        not BRANCH_RE.fullmatch(value)
        or ".." in value
        or "//" in value
        or "@{" in value
        or value == "@"
        or value.endswith(("/", ".", ".lock"))
        or any(
            component.startswith(".") or component.endswith(".lock")
            for component in value.split("/")
        )
    ):
        raise WorkspacePreflightError("branch_name_invalid")
    return value


def workspace_branch_name(task_id: str, execution_id: str) -> str:
    task = canonical_uuid(task_id, field="task_id").replace("-", "")
    execution = canonical_uuid(execution_id, field="execution_id").replace("-", "")
    return validate_branch(f"ai-orchestra/task-{task[:12]}/run-{execution}")


def workspace_path_for(
    workspace_id: str,
    *,
    root: Path = DEFAULT_OPENCODE_WORKSPACE_ROOT,
) -> str:
    canonical = canonical_uuid(workspace_id, field="workspace_id")
    if not root.is_absolute():
        raise WorkspacePreflightError("workspace_root_invalid")
    return str(root / canonical)


def opencode_directory_header(path: str) -> str:
    if not path.startswith("/") or "\x00" in path:
        raise WorkspacePreflightError("workspace_path_invalid")
    return quote(path, safe="")


def _manifest_payload(
    *,
    execution_id: str,
    task_id: str,
    repository_id: str,
    workspace_id: str,
    base_commit: str,
    branch_name: str,
    workspace_path: str,
    initial_tree: str,
    tracked_entries: int,
) -> dict[str, object]:
    if (
        isinstance(tracked_entries, bool)
        or not isinstance(tracked_entries, int)
        or tracked_entries < 0
    ):
        raise WorkspacePreflightError("tracked_entries_invalid")
    if (
        not isinstance(workspace_path, str)
        or not workspace_path.startswith("/")
        or "\x00" in workspace_path
    ):
        raise WorkspacePreflightError("workspace_path_invalid")
    return {
        "contract_version": WORKSPACE_CONTRACT_VERSION,
        "execution_id": canonical_uuid(execution_id, field="execution_id"),
        "task_id": canonical_uuid(task_id, field="task_id"),
        "repository_id": canonical_uuid(repository_id, field="repository_id"),
        "workspace_id": canonical_uuid(workspace_id, field="workspace_id"),
        "base_commit": normalize_commit(base_commit),
        "branch_name": validate_branch(branch_name),
        "workspace_path": workspace_path,
        "initial_tree": normalize_commit(initial_tree, field="initial_tree"),
        "tracked_entries": tracked_entries,
    }


def manifest_digest(payload: dict[str, object]) -> str:
    unsigned = dict(payload)
    unsigned.pop("preflight_digest", None)
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_manifest(**values: object) -> dict[str, object]:
    payload = _manifest_payload(**values)  # type: ignore[arg-type]
    payload["preflight_digest"] = manifest_digest(payload)
    return payload


def _safe_manifest_path(workspace: Path) -> Path:
    if workspace.is_symlink() or not workspace.is_dir():
        raise WorkspacePreflightError("workspace_path_invalid")
    git_directory = workspace / ".git"
    if git_directory.is_symlink() or not git_directory.is_dir():
        raise WorkspacePreflightError("workspace_git_directory_invalid")
    return git_directory / WORKSPACE_MANIFEST_NAME


def write_manifest(workspace: Path, payload: dict[str, object]) -> None:
    if set(payload) != MANIFEST_KEYS:
        raise WorkspacePreflightError("workspace_manifest_shape_invalid")
    if payload.get("preflight_digest") != manifest_digest(payload):
        raise WorkspacePreflightError("workspace_manifest_digest_invalid")
    manifest_path = _safe_manifest_path(workspace)
    temporary = manifest_path.with_name(f".{WORKSPACE_MANIFEST_NAME}.{uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        try:
            encoded = (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            if len(encoded) > 65536:
                raise WorkspacePreflightError("workspace_manifest_too_large")
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise WorkspacePreflightError("workspace_manifest_write_failed")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
        finally:
            os.close(descriptor)
        os.replace(temporary, manifest_path)
        directory_descriptor = os.open(manifest_path.parent, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_manifest(workspace: Path) -> dict[str, object]:
    directory_flags = (
        os.O_RDONLY
        | os.O_CLOEXEC
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        workspace_descriptor = os.open(workspace, directory_flags)
    except OSError as exc:
        raise WorkspacePreflightError("workspace_path_invalid") from exc
    try:
        workspace_metadata = os.fstat(workspace_descriptor)
        if not stat.S_ISDIR(workspace_metadata.st_mode):
            raise WorkspacePreflightError("workspace_path_invalid")
        try:
            git_descriptor = os.open(
                ".git",
                directory_flags,
                dir_fd=workspace_descriptor,
            )
        except OSError as exc:
            raise WorkspacePreflightError("workspace_git_directory_invalid") from exc
        try:
            git_metadata = os.fstat(git_descriptor)
            if not stat.S_ISDIR(git_metadata.st_mode):
                raise WorkspacePreflightError("workspace_git_directory_invalid")
            flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            try:
                manifest_descriptor = os.open(
                    WORKSPACE_MANIFEST_NAME,
                    flags,
                    dir_fd=git_descriptor,
                )
            except OSError as exc:
                raise WorkspacePreflightError("workspace_manifest_missing") from exc
            try:
                metadata = os.fstat(manifest_descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_size > 65536
                    or stat.S_IMODE(metadata.st_mode) != 0o400
                ):
                    raise WorkspacePreflightError("workspace_manifest_invalid")
                chunks = []
                remaining = 65537
                while remaining > 0:
                    chunk = os.read(manifest_descriptor, min(remaining, 16384))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                encoded = b"".join(chunks)
                if len(encoded) > 65536:
                    raise WorkspacePreflightError("workspace_manifest_invalid")
            finally:
                os.close(manifest_descriptor)
        finally:
            os.close(git_descriptor)
    finally:
        os.close(workspace_descriptor)
    try:
        payload = json.loads(encoded.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspacePreflightError("workspace_manifest_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != MANIFEST_KEYS:
        raise WorkspacePreflightError("workspace_manifest_shape_invalid")
    if (
        type(payload.get("contract_version")) is not int
        or payload.get("contract_version") != WORKSPACE_CONTRACT_VERSION
    ):
        raise WorkspacePreflightError("workspace_manifest_version_invalid")
    digest = payload.get("preflight_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise WorkspacePreflightError("workspace_manifest_digest_invalid")
    if digest != manifest_digest(payload):
        raise WorkspacePreflightError("workspace_manifest_digest_invalid")
    try:
        normalized = _manifest_payload(
            execution_id=payload["execution_id"],
            task_id=payload["task_id"],
            repository_id=payload["repository_id"],
            workspace_id=payload["workspace_id"],
            base_commit=payload["base_commit"],
            branch_name=payload["branch_name"],
            workspace_path=payload["workspace_path"],
            initial_tree=payload["initial_tree"],
            tracked_entries=payload["tracked_entries"],
        )
    except (KeyError, TypeError, ValueError, WorkspacePreflightError) as exc:
        raise WorkspacePreflightError("workspace_manifest_invalid") from exc
    if any(payload.get(key) != value for key, value in normalized.items()):
        raise WorkspacePreflightError("workspace_manifest_invalid")
    return payload


def git_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }


def safe_git_prefix(workspace: Path | None = None) -> list[str]:
    arguments = [
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.file.allow=always",
        "-c",
        "fetch.recurseSubmodules=false",
        "-c",
        "submodule.recurse=false",
    ]
    if workspace is not None:
        arguments.extend(["-C", str(workspace)])
    return arguments


def run_git(
    arguments: list[str],
    *,
    workspace: Path | None = None,
    timeout_seconds: int = 30,
    output_limit: int = 1024 * 1024,
    failure_code: str,
    binary: bool = False,
) -> str | bytes:
    try:
        process = subprocess.Popen(
            [*safe_git_prefix(workspace), *arguments],
            env=git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise WorkspacePreflightError("git_not_available") from exc
    try:
        stdout, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            process.kill()
        process.communicate()
        raise WorkspacePreflightError("git_operation_timed_out") from exc
    if process.returncode != 0:
        raise WorkspacePreflightError(failure_code)
    if len(stdout) > output_limit:
        raise WorkspacePreflightError("git_output_too_large")
    if binary:
        return stdout
    try:
        return stdout.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise WorkspacePreflightError("git_output_invalid") from exc


def validate_relative_git_path(raw_path: str) -> PurePosixPath:
    path = PurePosixPath(raw_path)
    if (
        not raw_path
        or raw_path.startswith("/")
        or raw_path.endswith("/")
        or len(raw_path.encode("utf-8")) > 4096
        or any(component in {"", ".", ".."} for component in path.parts)
    ):
        raise WorkspacePreflightError("repository_path_invalid")
    return path


def verify_tracked_files(
    workspace: Path,
    *,
    expected_count: int,
    max_files: int,
) -> None:
    raw = run_git(
        ["ls-files", "-z"],
        workspace=workspace,
        output_limit=max(1024 * 1024, max_files * 4096),
        failure_code="workspace_index_invalid",
        binary=True,
    )
    assert isinstance(raw, bytes)
    entries = raw.split(b"\x00")
    if entries and entries[-1] == b"":
        entries.pop()
    if len(entries) != expected_count or len(entries) > max_files:
        raise WorkspacePreflightError("workspace_tracked_entries_mismatch")
    root = workspace.resolve(strict=True)
    for raw_entry in entries:
        try:
            decoded = raw_entry.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise WorkspacePreflightError("repository_path_invalid") from exc
        relative = validate_relative_git_path(decoded)
        candidate = workspace.joinpath(*relative.parts)
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise WorkspacePreflightError("workspace_tracked_file_missing") from exc
        if stat.S_ISLNK(metadata.st_mode):
            try:
                resolved = candidate.resolve(strict=False)
                relative_target = resolved.relative_to(root)
            except (OSError, ValueError, RuntimeError) as exc:
                raise WorkspacePreflightError("workspace_symlink_escape") from exc
            if relative_target.parts and relative_target.parts[0] == ".git":
                raise WorkspacePreflightError("workspace_symlink_git_metadata")
        elif not stat.S_ISREG(metadata.st_mode):
            raise WorkspacePreflightError("workspace_tracked_type_invalid")


def verify_runtime_workspace(
    binding: WorkspaceBinding,
    *,
    workspace_root: Path = DEFAULT_OPENCODE_WORKSPACE_ROOT,
    max_files: int = 200_000,
) -> None:
    expected_path = workspace_path_for(binding.workspace_id, root=workspace_root)
    if binding.workspace_path != expected_path:
        raise WorkspacePreflightError("workspace_path_mismatch")
    workspace = Path(binding.workspace_path)
    try:
        expected_root = workspace_root.resolve(strict=True)
        if workspace.is_symlink() or workspace.resolve(strict=True) != expected_root / binding.workspace_id:
            raise WorkspacePreflightError("workspace_path_invalid")
    except (OSError, RuntimeError) as exc:
        raise WorkspacePreflightError("workspace_path_invalid") from exc

    payload = read_manifest(workspace)
    expected = {
        "contract_version": WORKSPACE_CONTRACT_VERSION,
        "execution_id": binding.execution_id,
        "task_id": binding.task_id,
        "repository_id": binding.repository_id,
        "workspace_id": binding.workspace_id,
        "base_commit": binding.base_commit,
        "branch_name": binding.branch_name,
        "workspace_path": binding.workspace_path,
        "initial_tree": binding.initial_tree,
        "tracked_entries": binding.tracked_entries,
        "preflight_digest": binding.preflight_digest,
    }
    if payload != expected:
        raise WorkspacePreflightError("workspace_manifest_binding_mismatch")

    head = str(
        run_git(
            ["rev-parse", "--verify", "HEAD^{commit}"],
            workspace=workspace,
            failure_code="workspace_head_invalid",
        )
    ).strip().lower()
    tree = str(
        run_git(
            ["rev-parse", "--verify", "HEAD^{tree}"],
            workspace=workspace,
            failure_code="workspace_tree_invalid",
        )
    ).strip().lower()
    branch = str(
        run_git(
            ["symbolic-ref", "--short", "HEAD"],
            workspace=workspace,
            failure_code="workspace_branch_invalid",
        )
    ).strip()
    status = run_git(
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
        ],
        workspace=workspace,
        output_limit=max(1024 * 1024, max_files * 4096),
        failure_code="workspace_status_invalid",
        binary=True,
    )
    if head != binding.base_commit:
        raise WorkspacePreflightError("workspace_head_mismatch")
    if tree != binding.initial_tree:
        raise WorkspacePreflightError("workspace_tree_mismatch")
    if branch != binding.branch_name:
        raise WorkspacePreflightError("workspace_branch_mismatch")
    if status:
        raise WorkspacePreflightError("workspace_not_clean")
    verify_tracked_files(
        workspace,
        expected_count=binding.tracked_entries,
        max_files=max_files,
    )
