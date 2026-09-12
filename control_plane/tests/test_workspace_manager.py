from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from control_plane.app.db import SessionLocal
from control_plane.app.execution_worker import ExecutionLease, ExecutionLeaseManager, poll_execution
from control_plane.app.models import AuditEvent, ExecutionRun, Repository, Task, TaskWorkspace
from control_plane.app.workspace_manager import (
    PreparedWorkspace,
    WorkspaceFilesystem,
    WorkspaceLease,
    WorkspaceLeaseManager,
    WorkspaceOperationError,
    process_workspace,
)
from control_plane.app.workspace_protocol import (
    WORKSPACE_MANIFEST_NAME,
    WorkspaceBinding,
    WorkspacePreflightError,
    build_manifest,
    manifest_digest,
    read_manifest,
    write_manifest,
)


def _git(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Workspace Test",
            "GIT_AUTHOR_EMAIL": "workspace@example.invalid",
            "GIT_COMMITTER_NAME": "Workspace Test",
            "GIT_COMMITTER_EMAIL": "workspace@example.invalid",
        },
    )
    return completed.stdout.strip()


def _build_mirror(
    root: Path,
    repository_id: str,
    *,
    dangerous_symlink: str | None = None,
    gitlink: bool = False,
) -> tuple[Path, str]:
    source = root / "source"
    source.mkdir(parents=True)
    _git(source, "init", "-b", "main")
    (source / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    (source / "README.md").write_text("workspace fixture\n", encoding="utf-8")
    if dangerous_symlink == "escape":
        (source / "outside").write_text("outside\n", encoding="utf-8")
        os.symlink("../outside", source / "escape")
    elif dangerous_symlink == "git":
        os.symlink(".git/config", source / "metadata-link")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    if gitlink:
        commit = _git(source, "rev-parse", "HEAD")
        _git(source, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/dependency")
        _git(source, "commit", "-m", "add forbidden gitlink")
    commit = _git(source, "rev-parse", "HEAD")

    mirror_root = root / "mirrors"
    mirror_root.mkdir()
    mirror = mirror_root / f"{repository_id}.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(mirror)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    (mirror_root / f".{repository_id}.sync.lock").touch(mode=0o600)
    return mirror_root, commit


def _lease(workspace_root: Path, repository_id: str, commit: str) -> WorkspaceLease:
    workspace_id = str(uuid4())
    return WorkspaceLease(
        workspace_id=workspace_id,
        execution_id=str(uuid4()),
        task_id=str(uuid4()),
        repository_id=repository_id,
        generation=1,
        record_version=2,
        operation="prepare",
        base_commit=commit,
        base_branch="main",
        branch_name=f"ai-orchestra/task-{uuid4().hex[:12]}/run-{uuid4().hex}",
        opencode_path=str(workspace_root / workspace_id),
        initial_tree=None,
        tracked_entries=None,
        preflight_digest=None,
    )


def _filesystem(mirror_root: Path, workspace_root: Path) -> WorkspaceFilesystem:
    return WorkspaceFilesystem(
        mirror_root,
        workspace_root,
        min_free_bytes=0,
        max_workspace_bytes=64 * 1024 * 1024,
    )


def _prepared_lease(lease: WorkspaceLease, result: PreparedWorkspace, operation: str) -> WorkspaceLease:
    return replace(
        lease,
        operation=operation,
        initial_tree=result.tree,
        tracked_entries=result.tracked_entries,
        preflight_digest=result.preflight_digest,
    )


def test_prepare_creates_clean_standalone_workspace_idempotently(tmp_path):
    repository_id = str(uuid4())
    mirror_root, commit = _build_mirror(tmp_path, repository_id)
    workspace_root = tmp_path / "workspaces"
    lease = _lease(workspace_root, repository_id, commit)
    filesystem = _filesystem(mirror_root, workspace_root)

    result = filesystem.prepare(lease, heartbeat=lambda: True)
    repeated = filesystem.prepare(lease, heartbeat=lambda: True)
    workspace = Path(lease.opencode_path)

    assert repeated == result
    assert _git(workspace, "rev-parse", "HEAD") == commit
    assert _git(workspace, "symbolic-ref", "--short", "HEAD") == lease.branch_name
    assert _git(workspace, "remote") == ""
    assert _git(workspace, "status", "--porcelain=v1", "--ignored=matching") == ""
    manifest = workspace / ".git" / WORKSPACE_MANIFEST_NAME
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o400
    assert read_manifest(workspace)["preflight_digest"] == result.preflight_digest


def test_manifest_reader_rejects_permission_link_and_type_tampering(tmp_path):
    repository_id = str(uuid4())
    mirror_root, commit = _build_mirror(tmp_path, repository_id)
    workspace_root = tmp_path / "workspaces"
    lease = _lease(workspace_root, repository_id, commit)
    filesystem = _filesystem(mirror_root, workspace_root)
    filesystem.prepare(lease, heartbeat=lambda: True)
    manifest = Path(lease.opencode_path) / ".git" / WORKSPACE_MANIFEST_NAME

    manifest.chmod(0o600)
    with pytest.raises(WorkspacePreflightError):
        read_manifest(Path(lease.opencode_path))
    manifest.chmod(0o400)

    hardlink = manifest.with_name("manifest-hardlink")
    os.link(manifest, hardlink)
    with pytest.raises(WorkspacePreflightError):
        read_manifest(Path(lease.opencode_path))
    hardlink.unlink()

    target = manifest.with_name("manifest-target")
    manifest.rename(target)
    manifest.symlink_to(target.name)
    with pytest.raises(WorkspacePreflightError):
        read_manifest(Path(lease.opencode_path))


def test_manifest_reader_rejects_boolean_contract_version(tmp_path):
    repository_id = str(uuid4())
    mirror_root, commit = _build_mirror(tmp_path, repository_id)
    workspace_root = tmp_path / "workspaces"
    lease = _lease(workspace_root, repository_id, commit)
    filesystem = _filesystem(mirror_root, workspace_root)
    filesystem.prepare(lease, heartbeat=lambda: True)
    workspace = Path(lease.opencode_path)
    payload = read_manifest(workspace)
    payload["contract_version"] = True
    payload["preflight_digest"] = manifest_digest(payload)
    write_manifest(workspace, payload)

    with pytest.raises(WorkspacePreflightError) as error:
        read_manifest(workspace)

    assert error.value.code == "workspace_manifest_version_invalid"


@pytest.mark.parametrize(
    ("dangerous_symlink", "gitlink", "expected_code"),
    [
        ("escape", False, "workspace_symlink_escape"),
        ("git", False, "workspace_symlink_git_metadata"),
        (None, True, "repository_submodule_forbidden"),
    ],
)
def test_prepare_rejects_unsafe_repository_shapes(
    tmp_path,
    dangerous_symlink,
    gitlink,
    expected_code,
):
    repository_id = str(uuid4())
    mirror_root, commit = _build_mirror(
        tmp_path,
        repository_id,
        dangerous_symlink=dangerous_symlink,
        gitlink=gitlink,
    )
    workspace_root = tmp_path / "workspaces"
    lease = _lease(workspace_root, repository_id, commit)

    with pytest.raises(WorkspaceOperationError) as error:
        _filesystem(mirror_root, workspace_root).prepare(lease, heartbeat=lambda: True)

    assert error.value.code == expected_code
    assert not Path(lease.opencode_path).exists()
    assert not list(workspace_root.glob(f".{lease.workspace_id}.*.prepare"))


def test_cleanup_removes_only_proven_clean_workspace_and_retains_changes(tmp_path):
    repository_id = str(uuid4())
    mirror_root, commit = _build_mirror(tmp_path, repository_id)
    workspace_root = tmp_path / "workspaces"
    filesystem = _filesystem(mirror_root, workspace_root)

    clean_lease = _lease(workspace_root, repository_id, commit)
    clean_result = filesystem.prepare(clean_lease, heartbeat=lambda: True)
    clean_lease = _prepared_lease(clean_lease, clean_result, "cleanup")
    assert filesystem.cleanup(clean_lease, heartbeat=lambda: True) is None
    assert not Path(clean_lease.opencode_path).exists()

    changed_lease = _lease(workspace_root, repository_id, commit)
    changed_result = filesystem.prepare(changed_lease, heartbeat=lambda: True)
    changed_lease = _prepared_lease(changed_lease, changed_result, "cleanup")
    (Path(changed_lease.opencode_path) / "result.txt").write_text("keep me\n", encoding="utf-8")
    inspection = filesystem.cleanup(changed_lease, heartbeat=lambda: True)
    assert inspection is not None and inspection.has_changes
    assert Path(changed_lease.opencode_path).is_dir()


def test_ignored_artifact_is_treated_as_a_change(tmp_path):
    repository_id = str(uuid4())
    mirror_root, commit = _build_mirror(tmp_path, repository_id)
    workspace_root = tmp_path / "workspaces"
    filesystem = _filesystem(mirror_root, workspace_root)
    lease = _lease(workspace_root, repository_id, commit)
    result = filesystem.prepare(lease, heartbeat=lambda: True)
    lease = _prepared_lease(lease, result, "inspect")
    (Path(lease.opencode_path) / "cache.tmp").write_text("ignored but material\n")

    inspection = filesystem.inspect(lease, heartbeat=lambda: True)

    assert inspection.has_changes is True
    assert inspection.changed_file_count == 1


def test_recalculated_manifest_cannot_replace_durable_database_binding(tmp_path):
    repository_id = str(uuid4())
    mirror_root, commit = _build_mirror(tmp_path, repository_id)
    workspace_root = tmp_path / "workspaces"
    filesystem = _filesystem(mirror_root, workspace_root)
    lease = _lease(workspace_root, repository_id, commit)
    result = filesystem.prepare(lease, heartbeat=lambda: True)
    lease = _prepared_lease(lease, result, "inspect")
    workspace = Path(lease.opencode_path)
    forged = build_manifest(
        execution_id=lease.execution_id,
        task_id=lease.task_id,
        repository_id=lease.repository_id,
        workspace_id=lease.workspace_id,
        base_commit=lease.base_commit,
        branch_name=lease.branch_name,
        workspace_path=lease.opencode_path,
        initial_tree="b" * 40,
        tracked_entries=result.tracked_entries,
    )
    write_manifest(workspace, forged)

    with pytest.raises(WorkspaceOperationError) as error:
        filesystem.inspect(lease, heartbeat=lambda: True)

    assert error.value.code == "workspace_manifest_binding_mismatch"


def _seed_preparing_workspace(*, mismatch: bool = False) -> tuple[str, str, str]:
    now = datetime.now(timezone.utc)
    repository_id = str(uuid4())
    task_id = str(uuid4())
    workspace_id = str(uuid4())
    execution_id = str(uuid4())
    path = f"/workspace/worktrees/managed/{workspace_id}"
    with SessionLocal() as db:
        db.add(
            Repository(
                id=repository_id,
                name=f"workspace-{repository_id}",
                remote_url=f"https://github.com/example/{repository_id}.git",
                remote_identity=f"github.com/example/{repository_id}",
                remote_host="github.com",
                provider="github",
                default_branch="main",
                enabled=True,
                status="ready",
                last_known_commit="a" * 40,
                last_fetched_at=now,
                sync_finished_at=now,
                sync_next_at=now,
            )
        )
        db.add(
            Task(
                id=task_id,
                title="Durable workspace lease",
                repository_id=repository_id,
                status="in_progress",
            )
        )
        db.add(
            TaskWorkspace(
                id=workspace_id,
                task_id=task_id,
                repository_id=repository_id,
                status="pending",
                base_commit="a" * 40,
                base_branch="main",
                branch_name=f"ai-orchestra/task-{task_id.replace('-', '')[:12]}/run-{execution_id.replace('-', '')}",
                opencode_path=path,
                requested_at=now,
                next_attempt_at=now,
            )
        )
        db.add(
            ExecutionRun(
                id=execution_id,
                task_id=task_id,
                contract_version=2,
                repository_id=repository_id,
                workspace_id=workspace_id,
                base_commit=("b" * 40 if mismatch else "a" * 40),
                workspace_path=path,
                status="preparing",
                stage="workspace_pending",
                deadline_at=now + timedelta(hours=1),
            )
        )
        db.commit()
    return task_id, workspace_id, execution_id


def test_stale_workspace_lease_is_recovered_and_old_generation_is_fenced():
    _, workspace_id, _ = _seed_preparing_workspace()
    t0 = datetime.now(timezone.utc)
    first = WorkspaceLeaseManager("workspace-a", lease_seconds=60)
    second = WorkspaceLeaseManager("workspace-b", lease_seconds=60)
    with SessionLocal() as db:
        [lease_a] = first.claim_available(db, limit=1, now=t0)
    with SessionLocal() as db:
        [lease_b] = second.claim_available(db, limit=1, now=t0 + timedelta(seconds=61))
    with SessionLocal() as db:
        assert first.heartbeat(db, lease_a, now=t0 + timedelta(seconds=62)) is False
        workspace = db.get(TaskWorkspace, workspace_id)
        audits = list(db.query(AuditEvent).all())

    assert lease_b.generation == lease_a.generation + 1
    assert workspace is not None and workspace.lease_owner == "workspace-b"
    assert any(event.action == "workspace.lease_recovered" for event in audits)


def test_database_binding_mismatch_fails_closed_and_is_audited():
    task_id, workspace_id, execution_id = _seed_preparing_workspace(mismatch=True)
    manager = WorkspaceLeaseManager("binding-check", lease_seconds=60)

    with SessionLocal() as db:
        assert manager.claim_available(db, limit=1) == []
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        workspace = db.get(TaskWorkspace, workspace_id)
        run = db.get(ExecutionRun, execution_id)
        actions = [event.action for event in db.query(AuditEvent).all()]

    assert task is not None and task.status == "failed"
    assert workspace is not None and workspace.status == "invalid"
    assert run is not None and run.status == "failed"
    assert run.stage == "workspace_binding_rejected"
    assert "workspace.binding_rejected" in actions


def test_durable_prepare_claim_publishes_filesystem_evidence_atomically(tmp_path):
    repository_id = str(uuid4())
    mirror_root, commit = _build_mirror(tmp_path, repository_id)
    workspace_root = tmp_path / "workspaces"
    requested = _lease(workspace_root, repository_id, commit)
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add(
            Repository(
                id=repository_id,
                name=f"prepare-{repository_id}",
                remote_url=f"https://github.com/example/{repository_id}.git",
                remote_identity=f"github.com/example/prepare-{repository_id}",
                remote_host="github.com",
                provider="github",
                default_branch="main",
                enabled=True,
                status="ready",
                last_known_commit=commit,
                last_fetched_at=now,
                sync_finished_at=now,
                sync_next_at=now,
            )
        )
        db.add(
            Task(
                id=requested.task_id,
                title="Atomic workspace prepare",
                repository_id=repository_id,
                status="in_progress",
            )
        )
        db.add(
            TaskWorkspace(
                id=requested.workspace_id,
                task_id=requested.task_id,
                repository_id=repository_id,
                status="pending",
                base_commit=commit,
                base_branch="main",
                branch_name=requested.branch_name,
                opencode_path=requested.opencode_path,
                requested_at=now,
                next_attempt_at=now,
            )
        )
        db.add(
            ExecutionRun(
                id=requested.execution_id,
                task_id=requested.task_id,
                contract_version=2,
                repository_id=repository_id,
                workspace_id=requested.workspace_id,
                base_commit=commit,
                workspace_path=requested.opencode_path,
                status="preparing",
                stage="workspace_pending",
                deadline_at=now + timedelta(hours=1),
            )
        )
        db.commit()

    manager = WorkspaceLeaseManager(
        "prepare-integration",
        lease_seconds=60,
        workspace_root=workspace_root,
    )
    filesystem = _filesystem(mirror_root, workspace_root)
    with SessionLocal() as db:
        [lease] = manager.claim_available(db, limit=1)

    assert process_workspace(manager, filesystem, lease) == "ready"
    with SessionLocal() as db:
        workspace = db.get(TaskWorkspace, requested.workspace_id)
        run = db.get(ExecutionRun, requested.execution_id)

    assert workspace is not None and workspace.status == "ready"
    assert workspace.initial_tree is not None
    assert workspace.preflight_digest is not None
    assert workspace.tracked_entries == 2
    assert run is not None and run.status == "queued"
    assert run.stage == "dispatch_pending"
    assert run.workspace_tree == workspace.initial_tree
    assert run.workspace_preflight_digest == workspace.preflight_digest
    assert Path(requested.opencode_path).is_dir()


class _NoInferenceClient:
    def __init__(self):
        self.create_calls = 0
        self.prompt_calls = 0
        self.directory = None

    def for_directory(self, directory):
        self.directory = directory
        return self

    def sessions_for_execution(self, _execution_id):
        return []

    def create_session(self, *_args, **_kwargs):
        self.create_calls += 1
        return {"id": "must-not-exist"}

    def prompt_async(self, *_args, **_kwargs):
        self.prompt_calls += 1


def _seed_ready_bound_execution(
    lease: WorkspaceLease,
    prepared: PreparedWorkspace,
    *,
    leased_by: str | None = None,
) -> datetime:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add(
            Repository(
                id=lease.repository_id,
                name=f"runtime-{lease.repository_id}",
                remote_url=f"https://github.com/example/{lease.repository_id}.git",
                remote_identity=f"github.com/example/runtime-{lease.repository_id}",
                remote_host="github.com",
                provider="github",
                default_branch="main",
                enabled=True,
                status="ready",
                last_known_commit=lease.base_commit,
                last_fetched_at=now,
                sync_finished_at=now,
                sync_next_at=now,
            )
        )
        db.add(
            Task(
                id=lease.task_id,
                title="Runtime workspace",
                repository_id=lease.repository_id,
                status="in_progress",
            )
        )
        db.add(
            TaskWorkspace(
                id=lease.workspace_id,
                task_id=lease.task_id,
                repository_id=lease.repository_id,
                status="ready",
                base_commit=lease.base_commit,
                base_branch="main",
                branch_name=lease.branch_name,
                opencode_path=lease.opencode_path,
                initial_tree=prepared.tree,
                preflight_digest=prepared.preflight_digest,
                tracked_entries=prepared.tracked_entries,
                current_head_commit=lease.base_commit,
                current_tree=prepared.tree,
                has_changes=False,
                changed_file_count=0,
                prepared_at=now,
                next_attempt_at=None,
            )
        )
        db.add(
            ExecutionRun(
                id=lease.execution_id,
                task_id=lease.task_id,
                contract_version=2,
                repository_id=lease.repository_id,
                workspace_id=lease.workspace_id,
                base_commit=lease.base_commit,
                workspace_path=lease.opencode_path,
                workspace_tree=prepared.tree,
                workspace_preflight_digest=prepared.preflight_digest,
                workspace_preflight_completed_at=now,
                status="queued",
                stage="dispatch_pending",
                lease_owner=leased_by,
                lease_generation=1 if leased_by else 0,
                heartbeat_at=now if leased_by else None,
                lease_expires_at=(now + timedelta(minutes=5) if leased_by else None),
                deadline_at=now + timedelta(hours=1),
            )
        )
        db.commit()
    return now


def test_runtime_tamper_blocks_inference_and_invalidates_binding(tmp_path):
    repository_id = str(uuid4())
    mirror_root, commit = _build_mirror(tmp_path, repository_id)
    workspace_root = tmp_path / "workspaces"
    filesystem = _filesystem(mirror_root, workspace_root)
    source_lease = _lease(workspace_root, repository_id, commit)
    prepared = filesystem.prepare(source_lease, heartbeat=lambda: True)
    binding = WorkspaceBinding(
        execution_id=source_lease.execution_id,
        task_id=source_lease.task_id,
        repository_id=source_lease.repository_id,
        workspace_id=source_lease.workspace_id,
        base_commit=source_lease.base_commit,
        branch_name=source_lease.branch_name,
        workspace_path=source_lease.opencode_path,
        initial_tree=prepared.tree,
        tracked_entries=prepared.tracked_entries,
        preflight_digest=prepared.preflight_digest,
    )
    now = _seed_ready_bound_execution(
        source_lease,
        prepared,
        leased_by="runtime-worker",
    )
    execution_lease = ExecutionLease(
        execution_id=source_lease.execution_id,
        generation=1,
        status="queued",
        opencode_session_id=None,
        deadline_at=now + timedelta(hours=1),
        cancel_requested_at=None,
        workspace_path=source_lease.opencode_path,
        workspace_binding=binding,
    )
    (Path(source_lease.opencode_path) / "README.md").write_text("tampered\n")
    fake = _NoInferenceClient()

    assert (
        poll_execution(
            ExecutionLeaseManager(
                "runtime-worker",
                lease_seconds=60,
                workspace_root=workspace_root,
            ),
            lambda: fake,
            execution_lease,
        )
        == "rejected"
    )
    with SessionLocal() as db:
        run = db.get(ExecutionRun, source_lease.execution_id)
        workspace = db.get(TaskWorkspace, source_lease.workspace_id)

    assert fake.directory == source_lease.opencode_path
    assert fake.create_calls == 0
    assert fake.prompt_calls == 0
    assert run is not None and run.status == "failed"
    assert run.stage == "workspace_binding_rejected"
    assert workspace is not None and workspace.status == "invalid"


class _SuccessfulInferenceClient:
    def __init__(self):
        self.directory = None
        self.session_id = "workspace-session"
        self.prompted = False
        self.create_calls = 0
        self.prompt_calls = 0

    def for_directory(self, directory):
        self.directory = directory
        return self

    def sessions_for_execution(self, _execution_id):
        return []

    def create_session(self, _title, *, metadata=None):
        self.create_calls += 1
        assert metadata
        return {"id": self.session_id, "metadata": metadata}

    def message(self, session_id, _message_id):
        assert session_id == self.session_id
        return {"info": {"role": "user"}} if self.prompted else None

    def prompt_async(self, session_id, _prompt, *, message_id, part_id):
        assert session_id == self.session_id
        assert message_id and part_id
        self.prompt_calls += 1
        self.prompted = True

    def session_statuses(self):
        return {self.session_id: {"type": "idle"}}

    def messages(self, session_id):
        assert session_id == self.session_id
        return [
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "workspace result"}],
            }
        ]


def test_verified_workspace_dispatches_and_terminal_run_queues_inspection(tmp_path):
    repository_id = str(uuid4())
    mirror_root, commit = _build_mirror(tmp_path, repository_id)
    workspace_root = tmp_path / "workspaces"
    filesystem = _filesystem(mirror_root, workspace_root)
    source_lease = _lease(workspace_root, repository_id, commit)
    prepared = filesystem.prepare(source_lease, heartbeat=lambda: True)
    _seed_ready_bound_execution(source_lease, prepared)
    manager = ExecutionLeaseManager(
        "runtime-success",
        lease_seconds=60,
        workspace_root=workspace_root,
    )
    fake = _SuccessfulInferenceClient()

    with SessionLocal() as db:
        [dispatch_lease] = manager.claim_available(db, limit=1)
    assert poll_execution(manager, lambda: fake, dispatch_lease) == "dispatched"

    with SessionLocal() as db:
        dispatched = db.get(ExecutionRun, source_lease.execution_id)
        assert dispatched is not None
        assert dispatched.status == "running"
        assert (
            dispatched.workspace_runtime_preflight_digest
            == prepared.preflight_digest
        )
        assert dispatched.workspace_runtime_verified_at is not None
        [completion_lease] = manager.claim_available(db, limit=1)

    assert poll_execution(manager, lambda: fake, completion_lease) == "completed"
    with SessionLocal() as db:
        run = db.get(ExecutionRun, source_lease.execution_id)
        task = db.get(Task, source_lease.task_id)
        workspace = db.get(TaskWorkspace, source_lease.workspace_id)
        actions = [event.action for event in db.query(AuditEvent).all()]

    assert fake.directory == source_lease.opencode_path
    assert fake.create_calls == 1
    assert fake.prompt_calls == 1
    assert run is not None and run.status == "completed"
    assert run.result == "workspace result"
    assert task is not None and task.status == "qa"
    assert workspace is not None and workspace.status == "inspection_pending"
    assert workspace.next_attempt_at is not None
    assert "execution.workspace_runtime_verified" in actions
    assert "workspace.inspection_requested" in actions


def test_binding_is_revalidated_before_prepare_result_is_committed():
    task_id, workspace_id, execution_id = _seed_preparing_workspace()
    manager = WorkspaceLeaseManager("binding-race", lease_seconds=60)
    with SessionLocal() as db:
        [lease] = manager.claim_available(db, limit=1)
    with SessionLocal() as db:
        run = db.get(ExecutionRun, execution_id)
        assert run is not None
        run.base_commit = "b" * 40
        db.commit()

    with SessionLocal() as db:
        outcome = manager.mark_prepare_success(
            db,
            lease,
            PreparedWorkspace(
                tree="c" * 40,
                tracked_entries=0,
                preflight_digest="d" * 64,
            ),
        )
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        workspace = db.get(TaskWorkspace, workspace_id)
        run = db.get(ExecutionRun, execution_id)

    assert outcome == "invalid"
    assert task is not None and task.status == "failed"
    assert workspace is not None and workspace.status == "invalid"
    assert run is not None and run.status == "failed"


def _constraint_binding(db):
    now = datetime.now(timezone.utc)
    repository_id = str(uuid4())
    task_id = str(uuid4())
    workspace_id = str(uuid4())
    execution_id = str(uuid4())
    path = f"/workspace/worktrees/managed/{workspace_id}"
    db.add(
        Repository(
            id=repository_id,
            name=f"constraint-{repository_id}",
            remote_url=f"https://github.com/example/{repository_id}.git",
            remote_identity=f"github.com/example/constraint-{repository_id}",
            remote_host="github.com",
            provider="github",
        )
    )
    db.add(
        Task(
            id=task_id,
            title="Workspace constraint",
            repository_id=repository_id,
            status="in_progress",
        )
    )
    db.flush()
    workspace = TaskWorkspace(
        id=workspace_id,
        task_id=task_id,
        repository_id=repository_id,
        status="pending",
        base_commit="a" * 40,
        base_branch="main",
        branch_name=(
            f"ai-orchestra/task-{task_id.replace('-', '')[:12]}"
            f"/run-{execution_id.replace('-', '')}"
        ),
        opencode_path=path,
        requested_at=now,
        next_attempt_at=now,
    )
    db.add(workspace)
    db.flush()
    return now, task_id, repository_id, workspace, execution_id, path


def test_database_constraints_reject_null_workspace_and_execution_evidence():
    with SessionLocal() as db:
        now, _, _, workspace, _, _ = _constraint_binding(db)
        workspace.status = "ready"
        workspace.next_attempt_at = None
        workspace.initial_tree = None
        workspace.preflight_digest = "b" * 64
        workspace.tracked_entries = 1
        workspace.prepared_at = now
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        now, task_id, repository_id, workspace, execution_id, _ = _constraint_binding(db)
        db.add(
            ExecutionRun(
                id=execution_id,
                task_id=task_id,
                contract_version=2,
                repository_id=repository_id,
                workspace_id=workspace.id,
                base_commit=None,
                workspace_path=None,
                status="preparing",
                stage="workspace_pending",
                deadline_at=now + timedelta(hours=1),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        now, task_id, repository_id, workspace, execution_id, path = _constraint_binding(db)
        db.add(
            ExecutionRun(
                id=execution_id,
                task_id=task_id,
                contract_version=2,
                repository_id=repository_id,
                workspace_id=workspace.id,
                base_commit="a" * 40,
                workspace_path=path,
                workspace_tree=None,
                workspace_preflight_digest=None,
                workspace_preflight_completed_at=now,
                status="queued",
                stage="dispatch_pending",
                deadline_at=now + timedelta(hours=1),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        now, task_id, repository_id, workspace, execution_id, path = _constraint_binding(db)
        db.add(
            ExecutionRun(
                id=execution_id,
                task_id=task_id,
                contract_version=2,
                repository_id=repository_id,
                workspace_id=workspace.id,
                base_commit="a" * 40,
                workspace_path=path,
                workspace_tree="b" * 40,
                workspace_preflight_digest="c" * 64,
                workspace_preflight_completed_at=now,
                workspace_runtime_preflight_digest=None,
                workspace_runtime_verified_at=now,
                status="running",
                stage="department_lead",
                deadline_at=now + timedelta(hours=1),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
