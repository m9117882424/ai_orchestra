from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import shutil
import socket
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .evidence import materialize_result_package, materialize_terminal_result_package
from .models import ExecutionRun, Repository, Task, TaskWorkspace
from .schema import assert_database_shape
from .services import write_audit
from .workspace_protocol import (
    DEFAULT_OPENCODE_WORKSPACE_ROOT,
    WorkspaceBinding,
    WorkspacePreflightError,
    build_manifest,
    canonical_uuid,
    normalize_commit,
    read_manifest,
    run_git,
    validate_branch,
    validate_relative_git_path,
    verify_runtime_workspace,
    workspace_path_for,
    write_manifest,
)


LOGGER = logging.getLogger("ai_orchestra.workspace_manager")
PREPARE_QUEUE_STATUSES = ("pending", "unavailable")
ACTIVE_WORKSPACE_STATUSES = ("preparing", "inspecting", "cleaning")
TERMINAL_EXECUTION_STATUSES = ("completed", "failed", "cancelled")
ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,79}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


class WorkspaceOperationError(RuntimeError):
    """A classified failure which never includes repository content or credentials."""

    def __init__(self, code: str, *, terminal: bool = False):
        if not ERROR_CODE_RE.fullmatch(code):
            raise ValueError("Unsafe workspace error code")
        super().__init__(code)
        self.code = code
        self.terminal = terminal


class WorkspaceLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceLease:
    workspace_id: str
    execution_id: str
    task_id: str
    repository_id: str
    generation: int
    record_version: int
    operation: str
    base_commit: str
    base_branch: str
    branch_name: str
    opencode_path: str
    initial_tree: str | None
    tracked_entries: int | None
    preflight_digest: str | None


@dataclass(frozen=True)
class PreparedWorkspace:
    tree: str
    tracked_entries: int
    preflight_digest: str


@dataclass(frozen=True)
class WorkspaceArtifact:
    path: str
    kind: str
    sha256: str | None
    size_bytes: int | None


@dataclass(frozen=True)
class WorkspaceInspection:
    head_commit: str
    tree: str
    change_digest: str
    has_changes: bool
    changed_file_count: int
    changed_files: tuple[str, ...]
    artifacts: tuple[WorkspaceArtifact, ...] = ()


def _changed_files_from_porcelain(raw_status: bytes) -> tuple[str, ...]:
    records = [record for record in raw_status.split(b"\x00") if record]
    changed: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        status_bytes = record[:3]
        path_bytes = record[3:]
        if len(status_bytes) != 3 or status_bytes[2:3] != b" ":
            raise WorkspaceOperationError("workspace_status_invalid", terminal=True)
        try:
            path = path_bytes.decode("utf-8", errors="strict")
            validate_relative_git_path(path)
        except (UnicodeError, WorkspacePreflightError) as exc:
            raise WorkspaceOperationError("workspace_status_invalid", terminal=True) from exc
        changed.append(path)

        if status_bytes[:1] in {b"R", b"C"}:
            index += 1
            if index >= len(records):
                raise WorkspaceOperationError("workspace_status_invalid", terminal=True)
            rename_bytes = records[index]
            try:
                rename_path = rename_bytes.decode("utf-8", errors="strict")
                validate_relative_git_path(rename_path)
            except (UnicodeError, WorkspacePreflightError) as exc:
                raise WorkspaceOperationError("workspace_status_invalid", terminal=True) from exc
            changed.append(rename_path)
        index += 1

    return tuple(sorted(set(changed)))


def _operation_for_status(status: str) -> str:
    if status in {*PREPARE_QUEUE_STATUSES, "preparing"}:
        return "prepare"
    if status in {"inspection_pending", "inspecting"}:
        return "inspect"
    if status in {"cleanup_pending", "cleaning"}:
        return "cleanup"
    raise WorkspaceOperationError("workspace_state_invalid", terminal=True)


def request_workspace_inspection(
    db: Session,
    run: ExecutionRun,
    *,
    actor: str,
    now: datetime,
) -> None:
    """Queue post-execution inspection in the caller's terminal-state transaction."""
    if run.contract_version != 2 or not run.workspace_id:
        return
    workspace = db.scalar(
        select(TaskWorkspace)
        .where(TaskWorkspace.id == run.workspace_id)
        .with_for_update()
    )
    if workspace is None:
        raise RuntimeError("Execution workspace binding is missing")
    if workspace.status in {"removed", "invalid", "inspection_pending", "inspecting"}:
        return
    if workspace.status != "ready":
        raise RuntimeError(
            f"Execution workspace cannot be inspected from state {workspace.status}"
        )
    workspace.status = "inspection_pending"
    workspace.inspection_requested_at = now
    workspace.next_attempt_at = now
    workspace.lease_owner = None
    workspace.lease_expires_at = None
    workspace.version += 1
    workspace.updated_at = now
    write_audit(
        db,
        actor=actor,
        action="workspace.inspection_requested",
        entity_type="workspace",
        entity_id=workspace.id,
        details={"execution_id": run.id, "task_id": run.task_id},
    )


class WorkspaceLeaseManager:
    """Durable lease, fencing and retry boundary for workspace lifecycle work."""

    def __init__(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 180,
        retry_base_seconds: int = 15,
        retry_max_seconds: int = 900,
        max_prepare_failures: int = 8,
        workspace_root: Path = DEFAULT_OPENCODE_WORKSPACE_ROOT,
    ):
        if lease_seconds < 60:
            raise ValueError("lease_seconds must be at least 60")
        if retry_base_seconds < 1 or retry_max_seconds < retry_base_seconds:
            raise ValueError("invalid retry interval")
        if not 1 <= max_prepare_failures <= 100:
            raise ValueError("max_prepare_failures must be between 1 and 100")
        if not workspace_root.is_absolute():
            raise ValueError("workspace_root must be absolute")
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.max_prepare_failures = max_prepare_failures
        self.workspace_root = workspace_root

    @property
    def audit_actor(self) -> str:
        return f"workspace-manager:{self.worker_id}"[:100]

    def _lease_deadline(self, now: datetime) -> datetime:
        return now + timedelta(seconds=self.lease_seconds)

    def _retry_at(self, failures: int, now: datetime) -> datetime:
        exponent = min(max(0, failures - 1), 20)
        delay = min(self.retry_max_seconds, self.retry_base_seconds * (2**exponent))
        return now + timedelta(seconds=delay)

    @staticmethod
    def _bound_execution(db: Session, workspace_id: str) -> ExecutionRun | None:
        return db.scalar(
            select(ExecutionRun)
            .where(ExecutionRun.workspace_id == workspace_id)
            .with_for_update()
        )

    @staticmethod
    def assert_repository_ready(
        db: Session,
        lease: WorkspaceLease,
    ) -> None:
        """Recheck repository trust before checkout and before queue publication."""
        repository = db.scalar(
            select(Repository)
            .where(Repository.id == lease.repository_id)
            .with_for_update()
        )
        if repository is None or not repository.enabled or repository.status == "invalid":
            raise WorkspaceOperationError("repository_trust_revoked", terminal=True)
        if (
            repository.status != "ready"
            or repository.default_branch is None
            or repository.last_known_commit is None
        ):
            raise WorkspaceOperationError("repository_not_ready")

    def _invalidate_binding(
        self,
        db: Session,
        workspace: TaskWorkspace,
        run: ExecutionRun | None,
        code: str,
        now: datetime,
    ) -> None:
        workspace.status = "invalid"
        workspace.next_attempt_at = None
        workspace.lease_owner = None
        workspace.lease_expires_at = None
        workspace.last_error_code = code
        workspace.version += 1
        workspace.updated_at = now
        execution_id = run.id if run is not None else None
        execution_became_terminal = False
        if run is not None and run.status in {"preparing", "queued", "running"}:
            run.status = "failed"
            execution_became_terminal = True
            run.stage = "workspace_binding_rejected"
            run.error = f"Workspace binding rejected: {code}"
            run.finished_at = now
            run.heartbeat_at = now
            run.lease_generation = int(run.lease_generation or 0) + 1
            run.lease_owner = None
            run.lease_expires_at = None
            run.updated_at = now
            task = db.get(Task, run.task_id)
            if task and task.status in {"in_progress", "waiting_approval"}:
                task.status = "failed"
                task.updated_at = now
        if execution_became_terminal and run is not None:
            materialize_terminal_result_package(db, run, now=now)
        write_audit(
            db,
            actor=self.audit_actor,
            action="workspace.binding_rejected",
            entity_type="workspace",
            entity_id=workspace.id,
            details={"execution_id": execution_id, "error_code": code},
        )

    def _binding_error(
        self,
        workspace: TaskWorkspace,
        run: ExecutionRun,
        operation: str,
    ) -> str | None:
        if (
            run.workspace_id != workspace.id
            or run.task_id != workspace.task_id
            or run.repository_id != workspace.repository_id
            or run.base_commit != workspace.base_commit
            or run.workspace_path != workspace.opencode_path
        ):
            return "workspace_database_binding_mismatch"
        try:
            canonical_uuid(workspace.id, field="workspace_id")
            canonical_uuid(workspace.task_id, field="task_id")
            canonical_uuid(workspace.repository_id, field="repository_id")
            canonical_uuid(run.id, field="execution_id")
            normalize_commit(workspace.base_commit)
            validate_branch(workspace.base_branch)
            validate_branch(workspace.branch_name)
            if workspace.opencode_path != workspace_path_for(
                workspace.id,
                root=self.workspace_root,
            ):
                return "workspace_path_mismatch"
        except WorkspacePreflightError as exc:
            return exc.code

        evidence = (
            workspace.initial_tree,
            workspace.tracked_entries,
            workspace.preflight_digest,
        )
        if operation == "inspect" and any(value is None for value in evidence):
            return "workspace_preflight_evidence_missing"
        if any(value is not None for value in evidence):
            if any(value is None for value in evidence):
                return "workspace_preflight_evidence_incomplete"
            try:
                tree = normalize_commit(str(workspace.initial_tree), field="initial_tree")
            except WorkspacePreflightError as exc:
                return exc.code
            tracked = workspace.tracked_entries
            digest = workspace.preflight_digest
            if (
                isinstance(tracked, bool)
                or not isinstance(tracked, int)
                or tracked < 0
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                return "workspace_preflight_evidence_invalid"
            if (
                run.workspace_tree != tree
                or run.workspace_preflight_digest != digest
                or run.workspace_preflight_completed_at is None
            ):
                return "workspace_execution_evidence_mismatch"
        return None

    def _expire_preparing_execution(
        self,
        db: Session,
        workspace: TaskWorkspace,
        run: ExecutionRun,
        now: datetime,
    ) -> None:
        run.status = "failed"
        run.stage = "workspace_preflight_timed_out"
        run.error = "Workspace preparation exceeded the durable execution deadline"
        run.finished_at = now
        run.heartbeat_at = now
        run.lease_generation = int(run.lease_generation or 0) + 1
        run.lease_owner = None
        run.lease_expires_at = None
        run.updated_at = now
        task = db.get(Task, run.task_id)
        if task and task.status in {"in_progress", "waiting_approval"}:
            task.status = "failed"
            task.updated_at = now
        workspace.status = "cleanup_pending"
        workspace.cleanup_requested_at = now
        workspace.next_attempt_at = now
        workspace.lease_owner = None
        workspace.lease_expires_at = None
        workspace.last_error_code = "execution_deadline_elapsed"
        workspace.version += 1
        workspace.updated_at = now
        materialize_terminal_result_package(db, run, now=now)
        write_audit(
            db,
            actor=self.audit_actor,
            action="execution.workspace_preflight_timed_out",
            entity_type="execution",
            entity_id=run.id,
            details={"task_id": run.task_id, "workspace_id": workspace.id},
        )

    def claim_available(
        self,
        db: Session,
        *,
        limit: int,
        now: datetime | None = None,
    ) -> list[WorkspaceLease]:
        if limit <= 0:
            return []
        now = now or utc_now()
        due_queued = and_(
            TaskWorkspace.status.in_(
                (*PREPARE_QUEUE_STATUSES, "inspection_pending", "cleanup_pending")
            ),
            TaskWorkspace.next_attempt_at.is_not(None),
            TaskWorkspace.next_attempt_at <= now,
        )
        expired_active = and_(
            TaskWorkspace.status.in_(ACTIVE_WORKSPACE_STATUSES),
            TaskWorkspace.lease_expires_at.is_not(None),
            TaskWorkspace.lease_expires_at <= now,
        )
        rows = list(
            db.scalars(
                select(TaskWorkspace)
                .where(or_(due_queued, expired_active))
                .order_by(TaskWorkspace.requested_at.asc())
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        leases: list[WorkspaceLease] = []
        for workspace in rows:
            run = self._bound_execution(db, workspace.id)
            if run is None or run.contract_version != 2:
                self._invalidate_binding(
                    db, workspace, run, "execution_binding_missing", now
                )
                continue

            operation = _operation_for_status(workspace.status)
            binding_error = self._binding_error(workspace, run, operation)
            if binding_error is not None:
                self._invalidate_binding(db, workspace, run, binding_error, now)
                continue
            if operation == "prepare":
                if run.cancel_requested_at is not None or run.status in TERMINAL_EXECUTION_STATUSES:
                    workspace.status = "cleanup_pending"
                    workspace.cleanup_requested_at = now
                    workspace.next_attempt_at = now
                    workspace.lease_owner = None
                    workspace.lease_expires_at = None
                    workspace.version += 1
                    workspace.updated_at = now
                    continue
                if run.status != "preparing":
                    self._invalidate_binding(
                        db, workspace, run, "preparation_execution_state_invalid", now
                    )
                    continue
                deadline = _as_utc(run.deadline_at)
                if deadline is None or deadline <= now:
                    self._expire_preparing_execution(db, workspace, run, now)
                    continue
            elif operation == "inspect" and run.status not in TERMINAL_EXECUTION_STATUSES:
                self._invalidate_binding(
                    db, workspace, run, "inspection_before_execution_terminal", now
                )
                continue
            elif operation == "cleanup" and run.status not in TERMINAL_EXECUTION_STATUSES:
                self._invalidate_binding(
                    db, workspace, run, "cleanup_before_execution_terminal", now
                )
                continue

            previous_status = workspace.status
            previous_owner = workspace.lease_owner
            previous_expiry = _as_utc(workspace.lease_expires_at)
            workspace.generation = int(workspace.generation or 0) + 1
            workspace.version += 1
            workspace.status = {
                "prepare": "preparing",
                "inspect": "inspecting",
                "cleanup": "cleaning",
            }[operation]
            workspace.started_at = now if operation == "prepare" else workspace.started_at
            workspace.next_attempt_at = None
            workspace.lease_owner = self.worker_id
            workspace.lease_expires_at = self._lease_deadline(now)
            workspace.updated_at = now
            action = (
                "workspace.lease_recovered"
                if previous_status in ACTIVE_WORKSPACE_STATUSES
                else f"workspace.{operation}_claimed"
            )
            write_audit(
                db,
                actor=self.audit_actor,
                action=action,
                entity_type="workspace",
                entity_id=workspace.id,
                details={
                    "execution_id": run.id,
                    "generation": workspace.generation,
                    "operation": operation,
                    "previous_owner": previous_owner,
                    "previous_lease_expires_at": (
                        previous_expiry.isoformat() if previous_expiry else None
                    ),
                },
            )
            leases.append(
                WorkspaceLease(
                    workspace_id=workspace.id,
                    execution_id=run.id,
                    task_id=workspace.task_id,
                    repository_id=workspace.repository_id,
                    generation=workspace.generation,
                    record_version=workspace.version,
                    operation=operation,
                    base_commit=workspace.base_commit,
                    base_branch=workspace.base_branch,
                    branch_name=workspace.branch_name,
                    opencode_path=workspace.opencode_path,
                    initial_tree=workspace.initial_tree,
                    tracked_entries=workspace.tracked_entries,
                    preflight_digest=workspace.preflight_digest,
                )
            )
        db.commit()
        return leases

    def _locked_owned_workspace(
        self,
        db: Session,
        lease: WorkspaceLease,
        now: datetime,
    ) -> TaskWorkspace | None:
        workspace = db.scalar(
            select(TaskWorkspace)
            .where(TaskWorkspace.id == lease.workspace_id)
            .with_for_update()
        )
        expiry = _as_utc(workspace.lease_expires_at) if workspace else None
        expected_status = {
            "prepare": "preparing",
            "inspect": "inspecting",
            "cleanup": "cleaning",
        }[lease.operation]
        if (
            workspace is None
            or workspace.status != expected_status
            or workspace.version != lease.record_version
            or workspace.generation != lease.generation
            or workspace.lease_owner != self.worker_id
            or expiry is None
            or expiry <= now
        ):
            return None
        return workspace

    def heartbeat(
        self,
        db: Session,
        lease: WorkspaceLease,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or utc_now()
        workspace = self._locked_owned_workspace(db, lease, now)
        if workspace is None:
            db.rollback()
            return False
        workspace.lease_expires_at = self._lease_deadline(now)
        workspace.updated_at = now
        db.commit()
        return True

    def mark_prepare_success(
        self,
        db: Session,
        lease: WorkspaceLease,
        result: PreparedWorkspace,
        *,
        now: datetime | None = None,
    ) -> str:
        try:
            tree = normalize_commit(result.tree, field="initial_tree")
        except WorkspacePreflightError as exc:
            raise WorkspaceOperationError(exc.code, terminal=True) from exc
        if (
            isinstance(result.tracked_entries, bool)
            or not isinstance(result.tracked_entries, int)
            or result.tracked_entries < 0
            or re.fullmatch(r"[0-9a-f]{64}", result.preflight_digest) is None
        ):
            raise WorkspaceOperationError("workspace_preflight_result_invalid", terminal=True)
        now = now or utc_now()
        workspace = self._locked_owned_workspace(db, lease, now)
        run = self._bound_execution(db, lease.workspace_id)
        if workspace is None or run is None:
            db.rollback()
            return "lost"
        binding_error = self._binding_error(workspace, run, "prepare")
        if binding_error is not None:
            self._invalidate_binding(db, workspace, run, binding_error, now)
            db.commit()
            return "invalid"
        self.assert_repository_ready(db, lease)
        deadline = _as_utc(run.deadline_at)
        if (
            run.status != "preparing"
            or run.cancel_requested_at is not None
            or run.repository_id != lease.repository_id
            or run.base_commit != lease.base_commit
            or run.workspace_path != lease.opencode_path
        ):
            db.rollback()
            return "lost"
        if deadline is None or deadline <= now:
            self._expire_preparing_execution(db, workspace, run, now)
            db.commit()
            return "expired"

        workspace.status = "ready"
        workspace.initial_tree = tree
        workspace.preflight_digest = result.preflight_digest
        workspace.tracked_entries = result.tracked_entries
        workspace.current_head_commit = lease.base_commit
        workspace.current_tree = workspace.initial_tree
        workspace.has_changes = False
        workspace.changed_file_count = 0
        workspace.prepared_at = now
        workspace.next_attempt_at = None
        workspace.lease_owner = None
        workspace.lease_expires_at = None
        workspace.failure_count = 0
        workspace.last_error_code = None
        workspace.version += 1
        workspace.updated_at = now

        run.status = "queued"
        run.stage = "dispatch_pending"
        run.workspace_tree = workspace.initial_tree
        run.workspace_preflight_digest = result.preflight_digest
        run.workspace_preflight_completed_at = now
        run.error = ""
        run.updated_at = now
        write_audit(
            db,
            actor=self.audit_actor,
            action="workspace.prepared",
            entity_type="workspace",
            entity_id=workspace.id,
            details={
                "execution_id": run.id,
                "repository_id": workspace.repository_id,
                "base_commit": workspace.base_commit,
                "tree": workspace.initial_tree,
                "tracked_entries": workspace.tracked_entries,
                "preflight_digest": workspace.preflight_digest,
            },
        )
        write_audit(
            db,
            actor=self.audit_actor,
            action="execution.queued",
            entity_type="execution",
            entity_id=run.id,
            details={
                "task_id": run.task_id,
                "repository_id": run.repository_id,
                "workspace_id": run.workspace_id,
                "base_commit": run.base_commit,
                "contract_version": run.contract_version,
            },
        )
        db.commit()
        return "ready"

    def mark_inspection_success(
        self,
        db: Session,
        lease: WorkspaceLease,
        result: WorkspaceInspection,
        *,
        now: datetime | None = None,
    ) -> str:
        result = self._validated_inspection(result)
        now = now or utc_now()
        workspace = self._locked_owned_workspace(db, lease, now)
        if workspace is None:
            db.rollback()
            return "lost"
        run = self._bound_execution(db, lease.workspace_id)
        if run is None:
            db.rollback()
            return "lost"
        binding_error = self._binding_error(workspace, run, "inspect")
        if binding_error is not None:
            self._invalidate_binding(db, workspace, run, binding_error, now)
            db.commit()
            return "invalid"
        workspace.status = "retained"
        workspace.current_head_commit = result.head_commit
        workspace.current_tree = result.tree
        workspace.change_digest = result.change_digest
        workspace.has_changes = result.has_changes
        workspace.changed_file_count = result.changed_file_count
        workspace.inspected_at = now
        workspace.next_attempt_at = None
        workspace.lease_owner = None
        workspace.lease_expires_at = None
        workspace.failure_count = 0
        workspace.last_error_code = None
        workspace.version += 1
        workspace.updated_at = now
        write_audit(
            db,
            actor=self.audit_actor,
            action="workspace.inspected",
            entity_type="workspace",
            entity_id=workspace.id,
            details={
                "execution_id": lease.execution_id,
                "head_commit": result.head_commit,
                "tree": result.tree,
                "has_changes": result.has_changes,
                "changed_file_count": result.changed_file_count,
                "changed_files": list(result.changed_files),
                "artifacts": [
                    {
                        "path": artifact.path,
                        "kind": artifact.kind,
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                    }
                    for artifact in result.artifacts
                ],
                "change_digest": result.change_digest,
            },
        )
        if run.status in TERMINAL_EXECUTION_STATUSES:
            db.flush()
            materialize_result_package(db, run.id, final=True, now=now)
        db.commit()
        return "retained"

    def mark_cleanup_success(
        self,
        db: Session,
        lease: WorkspaceLease,
        *,
        now: datetime | None = None,
    ) -> str:
        now = now or utc_now()
        workspace = self._locked_owned_workspace(db, lease, now)
        if workspace is None:
            db.rollback()
            return "lost"
        run = self._bound_execution(db, lease.workspace_id)
        if run is None:
            db.rollback()
            return "lost"
        binding_error = self._binding_error(workspace, run, "cleanup")
        if binding_error is not None:
            self._invalidate_binding(db, workspace, run, binding_error, now)
            db.commit()
            return "invalid"
        workspace.status = "removed"
        workspace.cleaned_at = now
        workspace.next_attempt_at = None
        workspace.lease_owner = None
        workspace.lease_expires_at = None
        workspace.last_error_code = None
        workspace.version += 1
        workspace.updated_at = now
        write_audit(
            db,
            actor=self.audit_actor,
            action="workspace.removed",
            entity_type="workspace",
            entity_id=workspace.id,
            details={"execution_id": lease.execution_id},
        )
        db.commit()
        return "removed"

    def mark_cleanup_retained(
        self,
        db: Session,
        lease: WorkspaceLease,
        result: WorkspaceInspection,
        *,
        now: datetime | None = None,
    ) -> str:
        result = self._validated_inspection(result)
        now = now or utc_now()
        workspace = self._locked_owned_workspace(db, lease, now)
        if workspace is None:
            db.rollback()
            return "lost"
        run = self._bound_execution(db, lease.workspace_id)
        if run is None:
            db.rollback()
            return "lost"
        binding_error = self._binding_error(workspace, run, "cleanup")
        if binding_error is not None:
            self._invalidate_binding(db, workspace, run, binding_error, now)
            db.commit()
            return "invalid"
        workspace.status = "retained"
        workspace.current_head_commit = result.head_commit
        workspace.current_tree = result.tree
        workspace.change_digest = result.change_digest
        workspace.has_changes = result.has_changes
        workspace.changed_file_count = result.changed_file_count
        workspace.inspected_at = now
        workspace.next_attempt_at = None
        workspace.lease_owner = None
        workspace.lease_expires_at = None
        workspace.last_error_code = "workspace_changed_cleanup_blocked"
        workspace.version += 1
        workspace.updated_at = now
        write_audit(
            db,
            actor=self.audit_actor,
            action="workspace.cleanup_blocked",
            entity_type="workspace",
            entity_id=workspace.id,
            details={
                "execution_id": lease.execution_id,
                "head_commit": result.head_commit,
                "tree": result.tree,
                "has_changes": result.has_changes,
                "changed_file_count": result.changed_file_count,
                "changed_files": list(result.changed_files),
                "artifacts": [
                    {
                        "path": artifact.path,
                        "kind": artifact.kind,
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                    }
                    for artifact in result.artifacts
                ],
                "change_digest": result.change_digest,
            },
        )
        db.commit()
        return "retained"

    @staticmethod
    def _validated_inspection(result: WorkspaceInspection) -> WorkspaceInspection:
        try:
            head = normalize_commit(result.head_commit, field="current_head_commit")
            tree = normalize_commit(result.tree, field="current_tree")
        except WorkspacePreflightError as exc:
            raise WorkspaceOperationError(exc.code, terminal=True) from exc
        if (
            not isinstance(result.has_changes, bool)
            or isinstance(result.changed_file_count, bool)
            or not isinstance(result.changed_file_count, int)
            or result.changed_file_count < 0
            or result.has_changes != (result.changed_file_count > 0)
            or re.fullmatch(r"[0-9a-f]{64}", result.change_digest) is None
        ):
            raise WorkspaceOperationError("workspace_inspection_result_invalid", terminal=True)
        changed_files: list[str] = []
        try:
            for path in result.changed_files:
                changed_files.append(str(validate_relative_git_path(path)))
        except (TypeError, WorkspacePreflightError) as exc:
            raise WorkspaceOperationError("workspace_inspection_result_invalid", terminal=True) from exc
        normalized_changed_files = tuple(sorted(set(changed_files)))

        artifacts: list[WorkspaceArtifact] = []
        for artifact in result.artifacts:
            if not isinstance(artifact, WorkspaceArtifact):
                raise WorkspaceOperationError("workspace_inspection_result_invalid", terminal=True)
            try:
                path = str(validate_relative_git_path(artifact.path))
            except (TypeError, WorkspacePreflightError) as exc:
                raise WorkspaceOperationError("workspace_inspection_result_invalid", terminal=True) from exc
            if artifact.kind not in {"file", "symlink", "deleted"}:
                raise WorkspaceOperationError("workspace_inspection_result_invalid", terminal=True)
            if artifact.kind == "deleted":
                if artifact.sha256 is not None or artifact.size_bytes is not None:
                    raise WorkspaceOperationError("workspace_inspection_result_invalid", terminal=True)
            elif (
                not isinstance(artifact.sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", artifact.sha256) is None
                or isinstance(artifact.size_bytes, bool)
                or not isinstance(artifact.size_bytes, int)
                or artifact.size_bytes < 0
            ):
                raise WorkspaceOperationError("workspace_inspection_result_invalid", terminal=True)
            artifacts.append(
                WorkspaceArtifact(
                    path=path,
                    kind=artifact.kind,
                    sha256=artifact.sha256,
                    size_bytes=artifact.size_bytes,
                )
            )
        normalized_artifacts = tuple(sorted(artifacts, key=lambda artifact: artifact.path))
        if tuple(artifact.path for artifact in normalized_artifacts) != normalized_changed_files:
            raise WorkspaceOperationError("workspace_inspection_result_invalid", terminal=True)

        return WorkspaceInspection(
            head_commit=head,
            tree=tree,
            change_digest=result.change_digest,
            has_changes=result.has_changes,
            changed_file_count=result.changed_file_count,
            changed_files=normalized_changed_files,
            artifacts=normalized_artifacts,
        )

    def mark_failure(
        self,
        db: Session,
        lease: WorkspaceLease,
        error: WorkspaceOperationError,
        *,
        now: datetime | None = None,
    ) -> str:
        now = now or utc_now()
        workspace = self._locked_owned_workspace(db, lease, now)
        if workspace is None:
            db.rollback()
            return "lost"
        run = self._bound_execution(db, lease.workspace_id)
        if run is None or run.contract_version != 2:
            self._invalidate_binding(
                db,
                workspace,
                run,
                "execution_binding_missing",
                now,
            )
            db.commit()
            return "invalid"
        binding_error = self._binding_error(workspace, run, lease.operation)
        if binding_error is not None:
            self._invalidate_binding(db, workspace, run, binding_error, now)
            db.commit()
            return "invalid"
        workspace.failure_count += 1
        workspace.lease_owner = None
        workspace.lease_expires_at = None
        workspace.last_error_code = error.code
        workspace.version += 1
        workspace.updated_at = now
        terminal = error.terminal
        if lease.operation == "prepare":
            terminal = terminal or workspace.failure_count >= self.max_prepare_failures
            deadline = _as_utc(run.deadline_at)
            terminal = terminal or deadline is None or deadline <= now
            if terminal:
                workspace.status = "invalid"
                workspace.next_attempt_at = None
                if run and run.status == "preparing":
                    run.status = "failed"
                    run.stage = "workspace_preflight_failed"
                    run.error = f"Workspace preflight failed: {error.code}"
                    run.finished_at = now
                    run.heartbeat_at = now
                    run.updated_at = now
                    task = db.get(Task, run.task_id)
                    if task and task.status in {"in_progress", "waiting_approval"}:
                        task.status = "failed"
                        task.updated_at = now
                    materialize_terminal_result_package(db, run, now=now)
            else:
                workspace.status = "unavailable"
                workspace.next_attempt_at = self._retry_at(workspace.failure_count, now)
                if run and run.status == "preparing":
                    run.stage = "workspace_retry_wait"
                    run.error = f"Workspace preflight retry pending: {error.code}"
                    run.updated_at = now
        elif terminal:
            workspace.status = "invalid"
            workspace.next_attempt_at = None
            if lease.operation == "inspect" and run.status in TERMINAL_EXECUTION_STATUSES:
                materialize_terminal_result_package(db, run, now=now)
        else:
            workspace.status = {
                "inspect": "inspection_pending",
                "cleanup": "cleanup_pending",
            }[lease.operation]
            workspace.next_attempt_at = self._retry_at(workspace.failure_count, now)

        write_audit(
            db,
            actor=self.audit_actor,
            action=f"workspace.{lease.operation}_failed",
            entity_type="workspace",
            entity_id=workspace.id,
            details={
                "execution_id": lease.execution_id,
                "generation": lease.generation,
                "error_code": error.code,
                "terminal": terminal,
                "retry_at": (
                    workspace.next_attempt_at.isoformat()
                    if workspace.next_attempt_at
                    else None
                ),
            },
        )
        db.commit()
        return workspace.status


class WorkspaceFilesystem:
    """Create and inspect standalone Git workspaces using local mirrors only."""

    def __init__(
        self,
        mirror_root: Path,
        workspace_root: Path,
        *,
        git_timeout_seconds: int = 60,
        max_files: int = 100_000,
        max_file_bytes: int = 128 * 1024 * 1024,
        max_workspace_bytes: int = 5 * 1024 * 1024 * 1024,
        min_free_bytes: int = 512 * 1024 * 1024,
    ):
        if git_timeout_seconds < 10:
            raise ValueError("git_timeout_seconds must be at least 10")
        if max_files < 1 or max_file_bytes < 1 or max_workspace_bytes < 1024 * 1024:
            raise ValueError("workspace limits must be positive")
        if min_free_bytes < 0:
            raise ValueError("min_free_bytes must be non-negative")
        self.mirror_root = mirror_root
        self.workspace_root = workspace_root
        self.git_timeout_seconds = git_timeout_seconds
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_workspace_bytes = max_workspace_bytes
        self.min_free_bytes = min_free_bytes

    @staticmethod
    def _ensure_owned_directory(path: Path, *, create: bool) -> None:
        if path.is_symlink():
            raise WorkspaceOperationError("storage_root_symlink", terminal=True)
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceOperationError("storage_root_unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise WorkspaceOperationError("storage_root_invalid", terminal=True)
        if create and stat.S_IMODE(metadata.st_mode) != 0o700:
            try:
                path.chmod(0o700)
            except OSError as exc:
                raise WorkspaceOperationError("storage_root_permissions", terminal=True) from exc

    def prepare_roots(self) -> None:
        self._ensure_owned_directory(self.mirror_root, create=False)
        self._ensure_owned_directory(self.workspace_root, create=True)

    @contextmanager
    def _workspace_lock(self, workspace_id: str) -> Iterator[None]:
        lock_path = self.workspace_root / f".{workspace_id}.lock"
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise WorkspaceOperationError("workspace_lock_invalid", terminal=True) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise WorkspaceOperationError("workspace_lock_invalid", terminal=True)
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkspaceOperationError("workspace_busy") from exc
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @contextmanager
    def _mirror_lock(self, repository_id: str) -> Iterator[Path]:
        mirror_path = self.mirror_root / f"{repository_id}.git"
        lock_path = self.mirror_root / f".{repository_id}.sync.lock"
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags)
        except OSError as exc:
            raise WorkspaceOperationError("repository_mirror_lock_unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise WorkspaceOperationError(
                    "repository_mirror_lock_invalid", terminal=True
                )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkspaceOperationError("repository_mirror_busy") from exc
            if mirror_path.is_symlink() or not mirror_path.is_dir():
                raise WorkspaceOperationError("repository_mirror_unavailable")
            try:
                yield mirror_path
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @staticmethod
    def _binding(lease: WorkspaceLease, result: PreparedWorkspace) -> WorkspaceBinding:
        return WorkspaceBinding(
            execution_id=lease.execution_id,
            task_id=lease.task_id,
            repository_id=lease.repository_id,
            workspace_id=lease.workspace_id,
            base_commit=lease.base_commit,
            branch_name=lease.branch_name,
            workspace_path=lease.opencode_path,
            initial_tree=result.tree,
            tracked_entries=result.tracked_entries,
            preflight_digest=result.preflight_digest,
        )

    def _heartbeat(self, heartbeat) -> None:
        if not heartbeat():
            raise WorkspaceLeaseLost()

    def _run_git(
        self,
        arguments: list[str],
        *,
        workspace: Path | None = None,
        failure_code: str,
        heartbeat,
        binary: bool = False,
        output_limit: int = 1024 * 1024,
    ):
        self._heartbeat(heartbeat)
        try:
            return run_git(
                arguments,
                workspace=workspace,
                timeout_seconds=self.git_timeout_seconds,
                output_limit=output_limit,
                failure_code=failure_code,
                binary=binary,
            )
        except WorkspacePreflightError as exc:
            raise WorkspaceOperationError(exc.code) from exc

    def _preflight_tree(self, mirror: Path, lease: WorkspaceLease, *, heartbeat) -> tuple[str, int]:
        commit = str(
            self._run_git(
                ["--git-dir", str(mirror), "rev-parse", "--verify", f"{lease.base_commit}^{{commit}}"],
                failure_code="base_commit_unavailable",
                heartbeat=heartbeat,
            )
        ).strip().lower()
        if commit != lease.base_commit:
            raise WorkspaceOperationError("base_commit_mismatch", terminal=True)
        tree = str(
            self._run_git(
                ["--git-dir", str(mirror), "rev-parse", "--verify", f"{lease.base_commit}^{{tree}}"],
                failure_code="base_tree_unavailable",
                heartbeat=heartbeat,
            )
        ).strip().lower()
        try:
            tree = normalize_commit(tree, field="initial_tree")
        except WorkspacePreflightError as exc:
            raise WorkspaceOperationError(exc.code, terminal=True) from exc

        raw = self._run_git(
            ["--git-dir", str(mirror), "ls-tree", "-r", "-l", "-z", lease.base_commit],
            failure_code="repository_tree_unavailable",
            heartbeat=heartbeat,
            binary=True,
            output_limit=64 * 1024 * 1024,
        )
        assert isinstance(raw, bytes)
        entries = raw.split(b"\x00")
        if entries and entries[-1] == b"":
            entries.pop()
        if len(entries) > self.max_files:
            raise WorkspaceOperationError("workspace_file_limit_exceeded", terminal=True)
        total = 0
        for raw_entry in entries:
            try:
                metadata, raw_path = raw_entry.split(b"\t", 1)
                mode, object_type, _object_id, raw_size = metadata.split(b" ", 3)
                path = raw_path.decode("utf-8", errors="strict")
                validate_relative_git_path(path)
            except (ValueError, UnicodeError, WorkspacePreflightError) as exc:
                raise WorkspaceOperationError("repository_tree_invalid", terminal=True) from exc
            if object_type == b"commit" or mode == b"160000":
                raise WorkspaceOperationError("repository_submodule_forbidden", terminal=True)
            if object_type != b"blob" or mode not in {b"100644", b"100755", b"120000"}:
                raise WorkspaceOperationError("repository_tree_entry_forbidden", terminal=True)
            try:
                size = int(raw_size)
            except ValueError as exc:
                raise WorkspaceOperationError("repository_tree_invalid", terminal=True) from exc
            if size < 0 or size > self.max_file_bytes:
                raise WorkspaceOperationError("workspace_file_size_limit_exceeded", terminal=True)
            total += size
            if total > self.max_workspace_bytes:
                raise WorkspaceOperationError("workspace_size_limit_exceeded", terminal=True)
        return tree, len(entries)

    def _verify_capacity(self) -> None:
        try:
            free = shutil.disk_usage(self.workspace_root).free
        except OSError as exc:
            raise WorkspaceOperationError("workspace_capacity_unavailable") from exc
        if free < self.min_free_bytes + self.max_workspace_bytes:
            raise WorkspaceOperationError("workspace_capacity_low")

    def _verify_total_size(self, path: Path, *, heartbeat) -> None:
        total = 0
        next_check = time.monotonic() + 5
        for root, directories, files in os.walk(path, followlinks=False):
            for name in directories:
                candidate = Path(root) / name
                metadata = candidate.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    # os.walk does not traverse directory symlinks when
                    # followlinks=False. Count the link itself; the subsequent
                    # runtime preflight validates that every tracked target stays
                    # inside this exact workspace and outside .git.
                    total += metadata.st_size
                    if total > self.max_workspace_bytes:
                        raise WorkspaceOperationError(
                            "workspace_size_limit_exceeded", terminal=True
                        )
            for name in files:
                candidate = Path(root) / name
                metadata = candidate.lstat()
                total += metadata.st_size
                if total > self.max_workspace_bytes:
                    raise WorkspaceOperationError("workspace_size_limit_exceeded", terminal=True)
                if time.monotonic() >= next_check:
                    self._heartbeat(heartbeat)
                    next_check = time.monotonic() + 5

    def _cleanup_prepare_staging(self, workspace_id: str) -> None:
        prefix = f".{workspace_id}."
        for candidate in self.workspace_root.iterdir():
            if not candidate.name.startswith(prefix) or not candidate.name.endswith(".prepare"):
                continue
            try:
                mode = candidate.lstat().st_mode
            except OSError as exc:
                raise WorkspaceOperationError("workspace_staging_unavailable") from exc
            if stat.S_ISLNK(mode) or stat.S_ISREG(mode):
                candidate.unlink()
            elif stat.S_ISDIR(mode):
                shutil.rmtree(candidate)
            else:
                raise WorkspaceOperationError(
                    "workspace_staging_type_invalid",
                    terminal=True,
                )

    def _prepared_from_existing(self, final_path: Path, lease: WorkspaceLease) -> PreparedWorkspace:
        try:
            payload = read_manifest(final_path)
            result = PreparedWorkspace(
                tree=str(payload["initial_tree"]),
                tracked_entries=int(payload["tracked_entries"]),
                preflight_digest=str(payload["preflight_digest"]),
            )
            verify_runtime_workspace(
                self._binding(lease, result),
                workspace_root=self.workspace_root,
                max_files=self.max_files,
            )
            return result
        except (WorkspacePreflightError, KeyError, TypeError, ValueError) as exc:
            code = exc.code if isinstance(exc, WorkspacePreflightError) else "workspace_manifest_invalid"
            raise WorkspaceOperationError(code, terminal=True) from exc

    def _read_bound_manifest(
        self,
        workspace: Path,
        lease: WorkspaceLease,
    ) -> dict[str, object]:
        try:
            payload = read_manifest(workspace)
        except WorkspacePreflightError as exc:
            raise WorkspaceOperationError(exc.code, terminal=True) from exc
        expected = {
            "execution_id": lease.execution_id,
            "task_id": lease.task_id,
            "repository_id": lease.repository_id,
            "workspace_id": lease.workspace_id,
            "base_commit": lease.base_commit,
            "branch_name": lease.branch_name,
            "workspace_path": lease.opencode_path,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise WorkspaceOperationError(
                "workspace_manifest_binding_mismatch", terminal=True
            )
        evidence = (lease.initial_tree, lease.tracked_entries, lease.preflight_digest)
        if any(value is not None for value in evidence):
            if any(value is None for value in evidence):
                raise WorkspaceOperationError(
                    "workspace_preflight_evidence_incomplete", terminal=True
                )
            if (
                payload.get("initial_tree") != lease.initial_tree
                or payload.get("tracked_entries") != lease.tracked_entries
                or payload.get("preflight_digest") != lease.preflight_digest
            ):
                raise WorkspaceOperationError(
                    "workspace_manifest_binding_mismatch", terminal=True
                )
        return payload

    def prepare(self, lease: WorkspaceLease, *, heartbeat) -> PreparedWorkspace:
        try:
            workspace_id = canonical_uuid(lease.workspace_id, field="workspace_id")
            repository_id = canonical_uuid(lease.repository_id, field="repository_id")
            canonical_uuid(lease.execution_id, field="execution_id")
            canonical_uuid(lease.task_id, field="task_id")
            normalize_commit(lease.base_commit)
            validate_branch(lease.base_branch)
            validate_branch(lease.branch_name)
        except WorkspacePreflightError as exc:
            raise WorkspaceOperationError(exc.code, terminal=True) from exc
        expected_path = workspace_path_for(workspace_id, root=self.workspace_root)
        if lease.opencode_path != expected_path:
            raise WorkspaceOperationError("workspace_path_mismatch", terminal=True)

        self.prepare_roots()
        with self._workspace_lock(workspace_id):
            final_path = self.workspace_root / workspace_id
            if final_path.exists() or final_path.is_symlink():
                return self._prepared_from_existing(final_path, lease)
            self._cleanup_prepare_staging(workspace_id)
            self._verify_capacity()
            with self._mirror_lock(repository_id) as mirror:
                tree, tracked_entries = self._preflight_tree(mirror, lease, heartbeat=heartbeat)
                staging = self.workspace_root / f".{workspace_id}.{uuid4().hex}.prepare"
                staging.mkdir(mode=0o700)
                try:
                    self._run_git(
                        ["init", str(staging)],
                        failure_code="workspace_initialization_failed",
                        heartbeat=heartbeat,
                    )
                    self._run_git(
                        ["remote", "add", "origin", str(mirror)],
                        workspace=staging,
                        failure_code="workspace_initialization_failed",
                        heartbeat=heartbeat,
                    )
                    self._run_git(
                        [
                            "fetch",
                            "--force",
                            "--no-tags",
                            "--no-write-fetch-head",
                            "--no-recurse-submodules",
                            "origin",
                            f"{lease.base_commit}:refs/ai-orchestra/base",
                        ],
                        workspace=staging,
                        failure_code="workspace_fetch_failed",
                        heartbeat=heartbeat,
                    )
                    self._run_git(
                        ["remote", "remove", "origin"],
                        workspace=staging,
                        failure_code="workspace_remote_removal_failed",
                        heartbeat=heartbeat,
                    )
                    self._run_git(
                        ["checkout", "--no-recurse-submodules", "-b", lease.branch_name, lease.base_commit],
                        workspace=staging,
                        failure_code="workspace_checkout_failed",
                        heartbeat=heartbeat,
                    )
                    for key, value in (
                        ("core.hooksPath", "/dev/null"),
                        ("core.fsmonitor", "false"),
                        ("protocol.allow", "never"),
                        ("protocol.file.allow", "never"),
                        ("fetch.recurseSubmodules", "false"),
                        ("submodule.recurse", "false"),
                    ):
                        self._run_git(
                            ["config", "--local", key, value],
                            workspace=staging,
                            failure_code="workspace_configuration_failed",
                            heartbeat=heartbeat,
                        )
                    result_payload = build_manifest(
                        execution_id=lease.execution_id,
                        task_id=lease.task_id,
                        repository_id=lease.repository_id,
                        workspace_id=lease.workspace_id,
                        base_commit=lease.base_commit,
                        branch_name=lease.branch_name,
                        workspace_path=lease.opencode_path,
                        initial_tree=tree,
                        tracked_entries=tracked_entries,
                    )
                    write_manifest(staging, result_payload)
                    result = PreparedWorkspace(
                        tree=tree,
                        tracked_entries=tracked_entries,
                        preflight_digest=str(result_payload["preflight_digest"]),
                    )
                    self._verify_total_size(staging, heartbeat=heartbeat)
                    self._heartbeat(heartbeat)
                    staging.rename(final_path)
                    root_fd = os.open(self.workspace_root, os.O_RDONLY | os.O_CLOEXEC)
                    try:
                        os.fsync(root_fd)
                    finally:
                        os.close(root_fd)
                    try:
                        verify_runtime_workspace(
                            self._binding(lease, result),
                            workspace_root=self.workspace_root,
                            max_files=self.max_files,
                        )
                    except WorkspacePreflightError as exc:
                        try:
                            final_path.rename(staging)
                            root_fd = os.open(
                                self.workspace_root, os.O_RDONLY | os.O_CLOEXEC
                            )
                            try:
                                os.fsync(root_fd)
                            finally:
                                os.close(root_fd)
                        except OSError as rollback_exc:
                            raise WorkspaceOperationError(
                                "workspace_activation_rollback_failed", terminal=True
                            ) from rollback_exc
                        raise WorkspaceOperationError(exc.code, terminal=True) from exc
                    return result
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)

    def inspect(self, lease: WorkspaceLease, *, heartbeat) -> WorkspaceInspection:
        workspace_id = canonical_uuid(lease.workspace_id, field="workspace_id")
        self.prepare_roots()
        with self._workspace_lock(workspace_id):
            self._heartbeat(heartbeat)
            return self.inspect_without_lock(lease, heartbeat=heartbeat)

    def cleanup(
        self,
        lease: WorkspaceLease,
        *,
        heartbeat,
    ) -> WorkspaceInspection | None:
        workspace_id = canonical_uuid(lease.workspace_id, field="workspace_id")
        self.prepare_roots()
        with self._workspace_lock(workspace_id):
            final_path = self.workspace_root / workspace_id
            trash = sorted(self.workspace_root.glob(f".{workspace_id}.*.remove"))
            if not final_path.exists() and not final_path.is_symlink():
                for candidate in trash:
                    try:
                        mode = candidate.lstat().st_mode
                    except OSError as exc:
                        raise WorkspaceOperationError(
                            "workspace_cleanup_state_unavailable"
                        ) from exc
                    if stat.S_ISLNK(mode) or stat.S_ISREG(mode):
                        candidate.unlink()
                    elif stat.S_ISDIR(mode):
                        shutil.rmtree(candidate)
                    else:
                        raise WorkspaceOperationError(
                            "workspace_cleanup_type_invalid",
                            terminal=True,
                        )
                return None

            inspection = self.inspect_without_lock(lease, heartbeat=heartbeat)
            payload = self._read_bound_manifest(final_path, lease)
            initial_tree = lease.initial_tree or str(payload["initial_tree"])
            if (
                inspection.has_changes
                or inspection.head_commit != lease.base_commit
                or inspection.tree != initial_tree
            ):
                return inspection

            self._heartbeat(heartbeat)
            trash_path = self.workspace_root / f".{workspace_id}.{uuid4().hex}.remove"
            final_path.rename(trash_path)
            root_fd = os.open(self.workspace_root, os.O_RDONLY | os.O_CLOEXEC)
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
            shutil.rmtree(trash_path)
            return None

    def _artifact_provenance(
        self,
        workspace: Path,
        changed_files: tuple[str, ...],
        *,
        heartbeat,
    ) -> tuple[WorkspaceArtifact, ...]:
        artifacts: list[WorkspaceArtifact] = []
        total_artifact_bytes = 0
        for relative_path in changed_files:
            self._heartbeat(heartbeat)
            try:
                normalized = str(validate_relative_git_path(relative_path))
            except WorkspacePreflightError as exc:
                raise WorkspaceOperationError("workspace_artifact_invalid", terminal=True) from exc
            candidate = workspace / normalized
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                artifacts.append(
                    WorkspaceArtifact(
                        path=normalized,
                        kind="deleted",
                        sha256=None,
                        size_bytes=None,
                    )
                )
                continue

            if metadata.st_size > self.max_file_bytes:
                raise WorkspaceOperationError(
                    "workspace_file_size_limit_exceeded", terminal=True
                )

            if stat.S_ISLNK(metadata.st_mode):
                try:
                    target = os.readlink(candidate)
                    repeated = candidate.lstat()
                except OSError as exc:
                    raise WorkspaceOperationError(
                        "workspace_artifact_changed_during_inspection", terminal=True
                    ) from exc
                if (
                    metadata.st_dev != repeated.st_dev
                    or metadata.st_ino != repeated.st_ino
                    or metadata.st_size != repeated.st_size
                    or metadata.st_mtime_ns != repeated.st_mtime_ns
                ):
                    raise WorkspaceOperationError(
                        "workspace_artifact_changed_during_inspection", terminal=True
                    )
                payload = os.fsencode(target)
                total_artifact_bytes += len(payload)
                if total_artifact_bytes > self.max_workspace_bytes:
                    raise WorkspaceOperationError("workspace_size_limit_exceeded", terminal=True)
                artifacts.append(
                    WorkspaceArtifact(
                        path=normalized,
                        kind="symlink",
                        sha256=hashlib.sha256(payload).hexdigest(),
                        size_bytes=len(payload),
                    )
                )
                continue

            if not stat.S_ISREG(metadata.st_mode):
                raise WorkspaceOperationError("workspace_artifact_type_unsupported", terminal=True)

            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(candidate, flags)
            except OSError as exc:
                raise WorkspaceOperationError(
                    "workspace_artifact_changed_during_inspection", terminal=True
                ) from exc
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_dev != metadata.st_dev
                    or opened.st_ino != metadata.st_ino
                ):
                    raise WorkspaceOperationError(
                        "workspace_artifact_changed_during_inspection", terminal=True
                    )
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
                    if total > self.max_file_bytes:
                        raise WorkspaceOperationError(
                            "workspace_file_size_limit_exceeded", terminal=True
                        )
                    self._heartbeat(heartbeat)
                closed = os.fstat(stream.fileno())
            if (
                opened.st_size != closed.st_size
                or opened.st_mtime_ns != closed.st_mtime_ns
                or total != closed.st_size
            ):
                raise WorkspaceOperationError(
                    "workspace_artifact_changed_during_inspection", terminal=True
                )
            total_artifact_bytes += total
            if total_artifact_bytes > self.max_workspace_bytes:
                raise WorkspaceOperationError("workspace_size_limit_exceeded", terminal=True)
            artifacts.append(
                WorkspaceArtifact(
                    path=normalized,
                    kind="file",
                    sha256=digest.hexdigest(),
                    size_bytes=total,
                )
            )
        return tuple(sorted(artifacts, key=lambda artifact: artifact.path))

    def inspect_without_lock(self, lease: WorkspaceLease, *, heartbeat) -> WorkspaceInspection:
        """Inspect while the caller already holds the per-workspace filesystem lock."""
        workspace = self.workspace_root / lease.workspace_id
        if workspace.is_symlink() or not workspace.is_dir():
            raise WorkspaceOperationError("workspace_unavailable")
        self._read_bound_manifest(workspace, lease)
        head = str(
            self._run_git(
                ["rev-parse", "--verify", "HEAD^{commit}"],
                workspace=workspace,
                failure_code="workspace_head_invalid",
                heartbeat=heartbeat,
            )
        ).strip().lower()
        tree = str(
            self._run_git(
                ["rev-parse", "--verify", "HEAD^{tree}"],
                workspace=workspace,
                failure_code="workspace_tree_invalid",
                heartbeat=heartbeat,
            )
        ).strip().lower()
        raw_status = self._run_git(
            [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            workspace=workspace,
            failure_code="workspace_status_invalid",
            heartbeat=heartbeat,
            binary=True,
            output_limit=64 * 1024 * 1024,
        )
        assert isinstance(raw_status, bytes)
        records = [record for record in raw_status.split(b"\x00") if record]
        if len(records) > self.max_files * 2:
            raise WorkspaceOperationError("workspace_change_limit_exceeded", terminal=True)
        changed_files = _changed_files_from_porcelain(raw_status)
        artifacts = self._artifact_provenance(
            workspace,
            changed_files,
            heartbeat=heartbeat,
        )
        try:
            head = normalize_commit(head, field="current_head_commit")
            tree = normalize_commit(tree, field="current_tree")
        except WorkspacePreflightError as exc:
            raise WorkspaceOperationError(exc.code, terminal=True) from exc
        return WorkspaceInspection(
            head_commit=head,
            tree=tree,
            change_digest=hashlib.sha256(raw_status).hexdigest(),
            has_changes=bool(records),
            changed_file_count=len(records),
            changed_files=changed_files,
            artifacts=artifacts,
        )


def process_workspace(
    manager: WorkspaceLeaseManager,
    filesystem: WorkspaceFilesystem,
    lease: WorkspaceLease,
) -> str:
    def heartbeat() -> bool:
        with SessionLocal() as db:
            return manager.heartbeat(db, lease)

    try:
        if lease.operation == "prepare":
            with SessionLocal() as db:
                manager.assert_repository_ready(db, lease)
                db.rollback()
            result = filesystem.prepare(lease, heartbeat=heartbeat)
            with SessionLocal() as db:
                return manager.mark_prepare_success(db, lease, result)
        if lease.operation == "inspect":
            inspection = filesystem.inspect(lease, heartbeat=heartbeat)
            with SessionLocal() as db:
                return manager.mark_inspection_success(db, lease, inspection)
        if lease.operation == "cleanup":
            inspection = filesystem.cleanup(lease, heartbeat=heartbeat)
            with SessionLocal() as db:
                if inspection is not None:
                    return manager.mark_cleanup_retained(db, lease, inspection)
                return manager.mark_cleanup_success(db, lease)
        raise WorkspaceOperationError("workspace_operation_invalid", terminal=True)
    except WorkspaceLeaseLost:
        LOGGER.warning(
            "Workspace operation lost lease workspace=%s generation=%s operation=%s",
            lease.workspace_id,
            lease.generation,
            lease.operation,
        )
        return "lost"
    except WorkspaceOperationError as exc:
        LOGGER.warning(
            "Workspace operation failed workspace=%s generation=%s operation=%s code=%s terminal=%s",
            lease.workspace_id,
            lease.generation,
            lease.operation,
            exc.code,
            exc.terminal,
        )
        with SessionLocal() as db:
            return manager.mark_failure(db, lease, exc)
    except Exception:
        LOGGER.exception(
            "Unexpected workspace operation failure workspace=%s generation=%s operation=%s",
            lease.workspace_id,
            lease.generation,
            lease.operation,
        )
        with SessionLocal() as db:
            return manager.mark_failure(db, lease, WorkspaceOperationError("internal_error"))


def write_worker_health(path: Path) -> None:
    path.touch(exist_ok=True)


def run_forever(
    manager: WorkspaceLeaseManager,
    filesystem: WorkspaceFilesystem,
    *,
    poll_seconds: int,
    max_active: int,
    health_path: Path,
) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=max_active, thread_name_prefix="workspace") as pool:
        while True:
            write_worker_health(health_path)
            with SessionLocal() as db:
                leases = manager.claim_available(db, limit=max_active)
            futures = {
                pool.submit(process_workspace, manager, filesystem, lease): lease
                for lease in leases
            }
            for future in as_completed(futures):
                lease = futures[future]
                try:
                    future.result()
                except Exception:
                    LOGGER.exception(
                        "Uncontained Workspace Manager failure workspace=%s generation=%s",
                        lease.workspace_id,
                        lease.generation,
                    )
            write_worker_health(health_path)
            time.sleep(poll_seconds)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    os.umask(0o077)
    worker_id = f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid4().hex[:12]}"
    lease_seconds = _bounded_int(
        "WORKSPACE_MANAGER_LEASE_SECONDS", 180, minimum=60, maximum=3600
    )
    git_timeout_seconds = _bounded_int(
        "WORKSPACE_MANAGER_GIT_TIMEOUT_SECONDS", 60, minimum=10, maximum=600
    )
    if lease_seconds < git_timeout_seconds + 30:
        raise RuntimeError(
            "WORKSPACE_MANAGER_LEASE_SECONDS must exceed Git timeout by 30s"
        )
    retry_base_seconds = _bounded_int(
        "WORKSPACE_MANAGER_RETRY_BASE_SECONDS", 15, minimum=1, maximum=3600
    )
    retry_max_seconds = _bounded_int(
        "WORKSPACE_MANAGER_RETRY_MAX_SECONDS", 900, minimum=1, maximum=86400
    )
    if retry_max_seconds < retry_base_seconds:
        raise RuntimeError("WORKSPACE_MANAGER_RETRY_MAX_SECONDS must be >= retry base")
    max_failures = _bounded_int(
        "WORKSPACE_MANAGER_MAX_PREPARE_FAILURES", 8, minimum=1, maximum=100
    )
    max_active = _bounded_int(
        "WORKSPACE_MANAGER_MAX_ACTIVE", 1, minimum=1, maximum=4
    )
    poll_seconds = _bounded_int(
        "WORKSPACE_MANAGER_POLL_SECONDS", 5, minimum=1, maximum=60
    )
    max_files = _bounded_int(
        "WORKSPACE_MANAGER_MAX_FILES", 100_000, minimum=1, maximum=1_000_000
    )
    max_file_bytes = _bounded_int(
        "WORKSPACE_MANAGER_MAX_FILE_BYTES",
        128 * 1024 * 1024,
        minimum=1024,
        maximum=1024 * 1024 * 1024,
    )
    max_workspace_bytes = _bounded_int(
        "WORKSPACE_MANAGER_MAX_WORKSPACE_BYTES",
        5 * 1024 * 1024 * 1024,
        minimum=1024 * 1024,
        maximum=1024 * 1024 * 1024 * 1024,
    )
    min_free_bytes = _bounded_int(
        "WORKSPACE_MANAGER_MIN_FREE_BYTES",
        512 * 1024 * 1024,
        minimum=64 * 1024 * 1024,
        maximum=1024 * 1024 * 1024 * 1024,
    )
    mirror_root = Path(
        os.getenv("WORKSPACE_MANAGER_MIRROR_ROOT", "/var/lib/ai-orchestra/repositories")
    )
    workspace_root = Path(
        os.getenv("WORKSPACE_MANAGER_STORAGE_ROOT", "/workspace/worktrees/managed")
    )
    health_path = Path(
        os.getenv(
            "WORKSPACE_MANAGER_HEALTH_PATH",
            "/tmp/ai-orchestra-workspace-manager.heartbeat",
        )
    )

    with SessionLocal() as db:
        assert_database_shape(db.get_bind())

    manager = WorkspaceLeaseManager(
        worker_id,
        lease_seconds=lease_seconds,
        retry_base_seconds=retry_base_seconds,
        retry_max_seconds=retry_max_seconds,
        max_prepare_failures=max_failures,
        workspace_root=workspace_root,
    )
    filesystem = WorkspaceFilesystem(
        mirror_root,
        workspace_root,
        git_timeout_seconds=git_timeout_seconds,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_workspace_bytes=max_workspace_bytes,
        min_free_bytes=min_free_bytes,
    )
    filesystem.prepare_roots()
    LOGGER.info(
        "Workspace Manager started worker_id=%s lease_seconds=%s max_active=%s poll_seconds=%s",
        worker_id,
        lease_seconds,
        max_active,
        poll_seconds,
    )
    run_forever(
        manager,
        filesystem,
        poll_seconds=poll_seconds,
        max_active=max_active,
        health_path=health_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
