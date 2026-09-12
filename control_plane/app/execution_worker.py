from __future__ import annotations

import logging
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .execution_protocol import (
    EXECUTION_METADATA_KEY,
    execution_message_id,
    execution_part_id,
    execution_prompt,
    execution_session_title,
)
from .models import ExecutionRun, Task, TaskWorkspace
from .opencode_client import (
    OpenCodeClient,
    OpenCodeError,
    OpenCodeNotFound,
    extract_last_assistant_text,
)
from .schema import assert_database_shape
from .services import write_audit
from .settings import get_settings
from .workspace_manager import request_workspace_inspection
from .workspace_protocol import (
    DEFAULT_OPENCODE_WORKSPACE_ROOT,
    WorkspaceBinding,
    WorkspacePreflightError,
    canonical_uuid,
    normalize_commit,
    validate_branch,
    verify_runtime_workspace,
    workspace_path_for,
)


LOGGER = logging.getLogger("ai_orchestra.execution_worker")
ACTIVE_EXECUTION_STATUSES = ("queued", "running")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _positive_int(name: str, default: int, *, minimum: int = 1, maximum: int = 10_000) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _positive_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class ExecutionLease:
    execution_id: str
    generation: int
    status: str
    opencode_session_id: str | None
    deadline_at: datetime
    cancel_requested_at: datetime | None
    workspace_path: str | None
    workspace_binding: WorkspaceBinding | None


class ExecutionLeaseManager:
    """PostgreSQL-backed lease and fencing boundary for dispatch and completion.

    Queued dispatch and running observation share the same lease generation. Any
    external side effect is reconciled before retry, while durable state may change
    only when the caller still owns the exact unexpired generation.
    """

    def __init__(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 120,
        execution_timeout_seconds: int = 7200,
        workspace_root: Path = DEFAULT_OPENCODE_WORKSPACE_ROOT,
        workspace_max_files: int = 100_000,
    ):
        if lease_seconds < 30:
            raise ValueError("lease_seconds must be at least 30")
        if not 60 <= execution_timeout_seconds <= 604800:
            raise ValueError("execution_timeout_seconds must be between 60 and 604800")
        if not workspace_root.is_absolute():
            raise ValueError("workspace_root must be absolute")
        if not 1 <= workspace_max_files <= 1_000_000:
            raise ValueError("workspace_max_files must be between 1 and 1000000")
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.execution_timeout_seconds = execution_timeout_seconds
        self.workspace_root = workspace_root
        self.workspace_max_files = workspace_max_files

    @property
    def audit_actor(self) -> str:
        return f"execution-worker:{self.worker_id}"[:100]

    def _deadline(self, now: datetime) -> datetime:
        return now + timedelta(seconds=self.lease_seconds)

    def _workspace_binding(
        self,
        db: Session,
        run: ExecutionRun,
    ) -> WorkspaceBinding | None:
        if run.contract_version == 1:
            return None
        if run.contract_version != 2 or not run.workspace_id:
            raise WorkspacePreflightError("workspace_binding_missing")
        workspace = db.scalar(
            select(TaskWorkspace)
            .where(TaskWorkspace.id == run.workspace_id)
            .with_for_update()
        )
        if workspace is None:
            raise WorkspacePreflightError("workspace_binding_missing")
        if workspace.status != "ready":
            raise WorkspacePreflightError("workspace_not_ready")
        if (
            workspace.task_id != run.task_id
            or workspace.repository_id != run.repository_id
            or workspace.base_commit != run.base_commit
            or workspace.opencode_path != run.workspace_path
            or workspace.initial_tree != run.workspace_tree
            or workspace.preflight_digest != run.workspace_preflight_digest
            or run.workspace_preflight_completed_at is None
        ):
            raise WorkspacePreflightError("workspace_database_binding_mismatch")
        canonical_uuid(run.id, field="execution_id")
        canonical_uuid(run.task_id, field="task_id")
        canonical_uuid(workspace.repository_id, field="repository_id")
        canonical_uuid(workspace.id, field="workspace_id")
        base_commit = normalize_commit(workspace.base_commit)
        initial_tree = normalize_commit(
            str(workspace.initial_tree), field="initial_tree"
        )
        branch_name = validate_branch(workspace.branch_name)
        if workspace.opencode_path != workspace_path_for(
            workspace.id,
            root=self.workspace_root,
        ):
            raise WorkspacePreflightError("workspace_path_mismatch")
        if (
            isinstance(workspace.tracked_entries, bool)
            or not isinstance(workspace.tracked_entries, int)
            or workspace.tracked_entries < 0
            or not isinstance(workspace.preflight_digest, str)
            or len(workspace.preflight_digest) != 64
            or any(character not in "0123456789abcdef" for character in workspace.preflight_digest)
        ):
            raise WorkspacePreflightError("workspace_preflight_evidence_invalid")
        return WorkspaceBinding(
            execution_id=run.id,
            task_id=run.task_id,
            repository_id=workspace.repository_id,
            workspace_id=workspace.id,
            base_commit=base_commit,
            branch_name=branch_name,
            workspace_path=workspace.opencode_path,
            initial_tree=initial_tree,
            tracked_entries=workspace.tracked_entries,
            preflight_digest=workspace.preflight_digest,
        )

    def _reject_invalid_workspace_binding(
        self,
        db: Session,
        run: ExecutionRun,
        code: str,
        now: datetime,
    ) -> None:
        run.status = "failed"
        run.stage = "workspace_binding_rejected"
        run.error = f"Workspace binding rejected: {code}"
        run.finished_at = now
        run.heartbeat_at = now
        run.lease_generation = int(run.lease_generation or 0) + 1
        run.lease_owner = None
        run.lease_expires_at = None
        run.updated_at = now
        workspace = db.get(TaskWorkspace, run.workspace_id) if run.workspace_id else None
        if workspace is not None:
            workspace.status = "invalid"
            workspace.next_attempt_at = None
            workspace.lease_owner = None
            workspace.lease_expires_at = None
            workspace.last_error_code = code
            workspace.version += 1
            workspace.updated_at = now
        task = db.get(Task, run.task_id)
        if task and task.status in {"in_progress", "waiting_approval"}:
            task.status = "failed"
            task.updated_at = now
        write_audit(
            db,
            actor=self.audit_actor,
            action="execution.workspace_binding_rejected",
            entity_type="execution",
            entity_id=run.id,
            details={
                "task_id": run.task_id,
                "workspace_id": run.workspace_id,
                "error_code": code,
            },
        )

    def claim_available(
        self,
        db: Session,
        *,
        limit: int,
        now: datetime | None = None,
    ) -> list[ExecutionLease]:
        if limit <= 0:
            return []
        now = now or utc_now()
        rows = list(
            db.scalars(
                select(ExecutionRun)
                .where(
                    ExecutionRun.status.in_(ACTIVE_EXECUTION_STATUSES),
                    or_(
                        ExecutionRun.lease_expires_at.is_(None),
                        ExecutionRun.lease_expires_at <= now,
                    ),
                )
                .order_by(ExecutionRun.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        leases: list[ExecutionLease] = []
        for run in rows:
            try:
                workspace_binding = self._workspace_binding(db, run)
            except (WorkspacePreflightError, TypeError, ValueError) as exc:
                code = (
                    exc.code
                    if isinstance(exc, WorkspacePreflightError)
                    else "workspace_binding_invalid"
                )
                self._reject_invalid_workspace_binding(db, run, code, now)
                continue
            if run.deadline_at is None:
                # Migration 0004 backfills every active production row. This
                # fallback also bounds rows created by old fixtures/manual tools.
                run.deadline_at = now + timedelta(seconds=self.execution_timeout_seconds)
            previous_generation = int(run.lease_generation or 0)
            previous_owner = run.lease_owner
            previous_expiry = _as_utc(run.lease_expires_at)
            run.lease_generation = previous_generation + 1
            run.lease_owner = self.worker_id
            run.heartbeat_at = now
            run.lease_expires_at = self._deadline(now)
            run.updated_at = now
            write_audit(
                db,
                actor=self.audit_actor,
                action=(
                    "execution.cancel_claimed"
                    if run.cancel_requested_at is not None
                    else (
                        "execution.lease_recovered"
                        if previous_generation > 0
                        else "execution.lease_claimed"
                    )
                ),
                entity_type="execution",
                entity_id=run.id,
                details={
                    "generation": run.lease_generation,
                    "status": run.status,
                    "previous_owner": previous_owner,
                    "previous_lease_expires_at": (
                        previous_expiry.isoformat() if previous_expiry else None
                    ),
                },
            )
            leases.append(
                ExecutionLease(
                    execution_id=run.id,
                    generation=run.lease_generation,
                    status=run.status,
                    opencode_session_id=run.opencode_session_id,
                    deadline_at=_as_utc(run.deadline_at) or now,
                    cancel_requested_at=_as_utc(run.cancel_requested_at),
                    workspace_path=run.workspace_path if run.contract_version == 2 else None,
                    workspace_binding=workspace_binding,
                )
            )
        db.commit()
        return leases

    def _locked_owned_run(
        self,
        db: Session,
        lease: ExecutionLease,
        now: datetime,
    ) -> ExecutionRun | None:
        run = db.scalar(
            select(ExecutionRun)
            .where(ExecutionRun.id == lease.execution_id)
            .with_for_update()
        )
        if run is None or run.status not in ACTIVE_EXECUTION_STATUSES:
            return None
        expires_at = _as_utc(run.lease_expires_at)
        if (
            run.lease_owner != self.worker_id
            or int(run.lease_generation or 0) != lease.generation
            or expires_at is None
            or expires_at <= now
        ):
            return None
        return run

    def heartbeat(
        self,
        db: Session,
        lease: ExecutionLease,
        *,
        now: datetime | None = None,
        allow_cancel_requested: bool = False,
    ) -> bool:
        now = now or utc_now()
        run = self._locked_owned_run(db, lease, now)
        if run is None:
            db.rollback()
            return False
        if run.cancel_requested_at is not None and not allow_cancel_requested:
            db.rollback()
            return False
        run.heartbeat_at = now
        run.lease_expires_at = self._deadline(now)
        run.updated_at = now
        db.commit()
        return True

    def persist_dispatch_session(
        self,
        db: Session,
        lease: ExecutionLease,
        session_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or utc_now()
        run = self._locked_owned_run(db, lease, now)
        if run is None or run.status != "queued":
            db.rollback()
            return False
        if run.cancel_requested_at is not None:
            db.rollback()
            return False
        if run.opencode_session_id and run.opencode_session_id != session_id:
            db.rollback()
            return False
        run.opencode_session_id = session_id
        run.stage = "dispatch_session_ready"
        run.heartbeat_at = now
        run.lease_expires_at = self._deadline(now)
        run.updated_at = now
        db.commit()
        return True

    def mark_runtime_workspace_verified(
        self,
        db: Session,
        lease: ExecutionLease,
        digest: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or utc_now()
        run = self._locked_owned_run(db, lease, now)
        if run is None or run.status != "queued":
            db.rollback()
            return False
        binding = lease.workspace_binding
        if (
            binding is None
            or digest != binding.preflight_digest
            or run.workspace_preflight_digest != digest
        ):
            db.rollback()
            return False
        first_verification = run.workspace_runtime_verified_at is None
        run.workspace_runtime_preflight_digest = digest
        run.workspace_runtime_verified_at = now
        run.stage = "dispatch_preflight_verified"
        run.heartbeat_at = now
        run.lease_expires_at = self._deadline(now)
        run.updated_at = now
        if first_verification:
            write_audit(
                db,
                actor=self.audit_actor,
                action="execution.workspace_runtime_verified",
                entity_type="execution",
                entity_id=run.id,
                details={
                    "task_id": run.task_id,
                    "workspace_id": run.workspace_id,
                    "preflight_digest": digest,
                    "generation": lease.generation,
                },
            )
        db.commit()
        return True

    def mark_runtime_workspace_rejected(
        self,
        db: Session,
        lease: ExecutionLease,
        code: str,
        *,
        now: datetime | None = None,
    ) -> str:
        now = now or utc_now()
        run = self._locked_owned_run(db, lease, now)
        if run is None:
            db.rollback()
            return "lost"
        self._reject_invalid_workspace_binding(db, run, code, now)
        db.commit()
        return "rejected"

    def request_safety_cancel(
        self,
        db: Session,
        lease: ExecutionLease,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or utc_now()
        run = self._locked_owned_run(db, lease, now)
        if run is None or run.status != "queued":
            db.rollback()
            return False
        if run.cancel_requested_at is None:
            run.cancel_requested_at = now
            run.stage = "safety_abort_requested"
            run.error = (
                "A pre-existing OpenCode message has no durable runtime workspace "
                "verification evidence"
            )
            run.heartbeat_at = now
            run.lease_expires_at = self._deadline(now)
            run.updated_at = now
            write_audit(
                db,
                actor=self.audit_actor,
                action="execution.safety_abort_requested",
                entity_type="execution",
                entity_id=run.id,
                details={
                    "task_id": run.task_id,
                    "workspace_id": run.workspace_id,
                    "generation": lease.generation,
                },
            )
        db.commit()
        return True

    def mark_dispatched(
        self,
        db: Session,
        lease: ExecutionLease,
        session_id: str,
        message_id: str,
        part_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or utc_now()
        run = self._locked_owned_run(db, lease, now)
        if run is None or run.status != "queued":
            db.rollback()
            return False
        if run.cancel_requested_at is not None:
            db.rollback()
            return False
        if run.opencode_session_id != session_id:
            db.rollback()
            return False
        if run.contract_version == 2 and (
            run.workspace_runtime_preflight_digest != run.workspace_preflight_digest
            or run.workspace_runtime_verified_at is None
        ):
            db.rollback()
            return False
        run.status = "running"
        run.stage = "department_lead"
        run.error = ""
        run.heartbeat_at = now
        run.lease_owner = None
        run.lease_expires_at = None
        run.updated_at = now
        write_audit(
            db,
            actor=self.audit_actor,
            action="execution.dispatched",
            entity_type="execution",
            entity_id=run.id,
            details={
                "task_id": run.task_id,
                "generation": lease.generation,
                "session_id": session_id,
                "message_id": message_id,
                "part_id": part_id,
            },
        )
        db.commit()
        return True

    def apply_observation(
        self,
        db: Session,
        lease: ExecutionLease,
        *,
        state_type: str,
        result: str,
        now: datetime | None = None,
    ) -> str:
        now = now or utc_now()
        run = self._locked_owned_run(db, lease, now)
        if run is None or run.status != "running":
            db.rollback()
            return "lost"

        if run.cancel_requested_at is not None:
            db.rollback()
            return "cancel_requested"

        deadline_at = _as_utc(run.deadline_at)
        if deadline_at is not None and deadline_at <= now:
            run.heartbeat_at = now
            run.lease_expires_at = self._deadline(now)
            run.updated_at = now
            db.commit()
            return "deadline"

        if state_type == "idle" and result.strip():
            run.status = "completed"
            run.stage = "manager_review"
            run.result = result.strip()
            run.error = ""
            run.finished_at = now
            run.heartbeat_at = now
            run.lease_owner = None
            run.lease_expires_at = None
            run.updated_at = now
            task = db.get(Task, run.task_id)
            if task and task.status in {"in_progress", "waiting_approval"}:
                task.status = "qa"
                task.updated_at = now
            request_workspace_inspection(
                db,
                run,
                actor=self.audit_actor,
                now=now,
            )
            write_audit(
                db,
                actor=self.audit_actor,
                action="execution.completed",
                entity_type="execution",
                entity_id=run.id,
                details={
                    "task_id": run.task_id,
                    "generation": lease.generation,
                },
            )
            db.commit()
            return "completed"

        run.heartbeat_at = now
        run.lease_expires_at = self._deadline(now)
        run.updated_at = now
        db.commit()
        return "running"

    def mark_timeout_pending(
        self,
        db: Session,
        lease: ExecutionLease,
        error: str,
        *,
        now: datetime | None = None,
    ) -> str:
        """Keep an overdue execution active until external abort is confirmed."""
        now = now or utc_now()
        run = self._locked_owned_run(db, lease, now)
        if run is None:
            db.rollback()
            return "lost"
        first_attempt = run.stage != "timeout_abort_pending"
        run.stage = "timeout_abort_pending"
        run.error = (
            "Execution deadline exceeded; OpenCode abort is not yet confirmed: "
            + error[:1000]
        )
        run.heartbeat_at = now
        run.lease_expires_at = self._deadline(now)
        run.updated_at = now
        if first_attempt:
            write_audit(
                db,
                actor=self.audit_actor,
                action="execution.timeout_abort_pending",
                entity_type="execution",
                entity_id=run.id,
                details={
                    "task_id": run.task_id,
                    "generation": lease.generation,
                    "deadline_at": (
                        _as_utc(run.deadline_at).isoformat() if run.deadline_at else None
                    ),
                },
            )
        db.commit()
        return "running"

    def mark_timed_out(
        self,
        db: Session,
        lease: ExecutionLease,
        *,
        aborted_session_ids: list[str],
        now: datetime | None = None,
    ) -> str:
        """Commit timeout only while this exact lease generation still owns the row."""
        now = now or utc_now()
        run = self._locked_owned_run(db, lease, now)
        if run is None:
            db.rollback()
            return "lost"
        previous_status = run.status
        deadline_at = _as_utc(run.deadline_at)
        run.status = "failed"
        run.stage = "timed_out"
        run.error = (
            "Execution exceeded its durable deadline"
            + (f" ({deadline_at.isoformat()})" if deadline_at else "")
        )
        run.finished_at = now
        run.heartbeat_at = now
        run.lease_owner = None
        run.lease_expires_at = None
        run.updated_at = now
        task = db.get(Task, run.task_id)
        if task and task.status in {"in_progress", "waiting_approval"}:
            task.status = "failed"
            task.updated_at = now
        request_workspace_inspection(
            db,
            run,
            actor=self.audit_actor,
            now=now,
        )
        write_audit(
            db,
            actor=self.audit_actor,
            action="execution.timed_out",
            entity_type="execution",
            entity_id=run.id,
            details={
                "task_id": run.task_id,
                "generation": lease.generation,
                "previous_status": previous_status,
                "deadline_at": deadline_at.isoformat() if deadline_at else None,
                "aborted_session_ids": aborted_session_ids,
            },
        )
        db.commit()
        return "timed_out"

    def mark_cancel_pending(
        self,
        db: Session,
        lease: ExecutionLease,
        error: str,
        *,
        now: datetime | None = None,
    ) -> str:
        """Retain durable cancellation intent until cleanup is confirmed."""
        now = now or utc_now()
        run = self._locked_owned_run(db, lease, now)
        if run is None or run.cancel_requested_at is None:
            db.rollback()
            return "lost"
        first_attempt = run.stage != "cancel_cleanup_pending"
        run.stage = "cancel_cleanup_pending"
        run.error = "Cancellation cleanup is not yet confirmed: " + error[:1000]
        run.heartbeat_at = now
        run.lease_expires_at = self._deadline(now)
        run.updated_at = now
        if first_attempt:
            write_audit(
                db,
                actor=self.audit_actor,
                action="execution.cancel_cleanup_pending",
                entity_type="execution",
                entity_id=run.id,
                details={
                    "task_id": run.task_id,
                    "generation": lease.generation,
                    "cancel_requested_at": _as_utc(run.cancel_requested_at).isoformat(),
                },
            )
        db.commit()
        return "running"

    def mark_cancelled(
        self,
        db: Session,
        lease: ExecutionLease,
        *,
        cleaned_session_ids: list[str],
        now: datetime | None = None,
    ) -> str:
        now = now or utc_now()
        run = self._locked_owned_run(db, lease, now)
        if run is None or run.cancel_requested_at is None:
            db.rollback()
            return "lost"
        previous_status = run.status
        run.status = "cancelled"
        run.stage = "stopped"
        run.error = ""
        run.finished_at = now
        run.heartbeat_at = now
        run.lease_owner = None
        run.lease_expires_at = None
        run.updated_at = now
        task = db.get(Task, run.task_id)
        if task and task.status in {"in_progress", "waiting_approval"}:
            task.status = "failed"
            task.updated_at = now
        request_workspace_inspection(
            db,
            run,
            actor=self.audit_actor,
            now=now,
        )
        write_audit(
            db,
            actor=self.audit_actor,
            action="execution.cancelled",
            entity_type="execution",
            entity_id=run.id,
            details={
                "task_id": run.task_id,
                "generation": lease.generation,
                "previous_status": previous_status,
                "cleaned_session_ids": cleaned_session_ids,
            },
        )
        db.commit()
        return "cancelled"


def _queued_dispatch_context(
    manager: ExecutionLeaseManager,
    lease: ExecutionLease,
) -> tuple[str | None, str, str, bool] | None:
    """Read a fenced queued run without holding its DB transaction over network I/O."""
    with SessionLocal() as db:
        now = utc_now()
        run = manager._locked_owned_run(db, lease, now)
        if run is None or run.status != "queued":
            db.rollback()
            return None
        task = db.get(Task, run.task_id)
        if task is None:
            db.rollback()
            return None
        session_id = run.opencode_session_id
        title = execution_session_title(task, run.id)
        prompt = execution_prompt(task)
        runtime_verified = run.contract_version == 1 or (
            run.workspace_runtime_preflight_digest
            == run.workspace_preflight_digest
            and run.workspace_runtime_verified_at is not None
        )
        db.rollback()
        return session_id, title, prompt, runtime_verified


def _verify_workspace_before_inference(
    manager: ExecutionLeaseManager,
    lease: ExecutionLease,
) -> bool:
    binding = lease.workspace_binding
    if binding is None:
        return True
    try:
        verify_runtime_workspace(
            binding,
            workspace_root=manager.workspace_root,
            max_files=manager.workspace_max_files,
        )
    except (WorkspacePreflightError, OSError) as exc:
        code = (
            exc.code
            if isinstance(exc, WorkspacePreflightError)
            else "workspace_runtime_unavailable"
        )
        LOGGER.error(
            "Runtime workspace rejected execution=%s generation=%s code=%s",
            lease.execution_id,
            lease.generation,
            code,
        )
        with SessionLocal() as db:
            manager.mark_runtime_workspace_rejected(db, lease, code)
        return False
    with SessionLocal() as db:
        return manager.mark_runtime_workspace_verified(
            db,
            lease,
            binding.preflight_digest,
        )


def _renew_before_external_side_effect(
    manager: ExecutionLeaseManager,
    lease: ExecutionLease,
) -> bool:
    """Fence an external POST with a lease renewed immediately before the call."""
    with SessionLocal() as db:
        return manager.heartbeat(db, lease)


def _renew_before_cleanup_side_effect(
    manager: ExecutionLeaseManager,
    lease: ExecutionLease,
) -> bool:
    with SessionLocal() as db:
        return manager.heartbeat(db, lease, allow_cancel_requested=True)


def _deadline_elapsed(
    lease: ExecutionLease,
    *,
    now: datetime | None = None,
) -> bool:
    deadline_at = _as_utc(lease.deadline_at)
    return deadline_at is not None and deadline_at <= (now or utc_now())


def cancel_execution(
    manager: ExecutionLeaseManager,
    client: OpenCodeClient,
    lease: ExecutionLease,
) -> str:
    """Reconcile durable cancellation and then commit the terminal state."""
    with SessionLocal() as db:
        now = utc_now()
        run = manager._locked_owned_run(db, lease, now)
        if run is None or run.cancel_requested_at is None:
            db.rollback()
            return "lost"
        session_id = run.opencode_session_id
        status = run.status
        db.rollback()

    try:
        session_ids: list[str] = []
        if session_id:
            session_ids.append(session_id)
        else:
            for session in client.sessions_for_execution(lease.execution_id):
                recovered_id = str(session.get("id") or session.get("sessionID") or "")
                if not recovered_id:
                    raise OpenCodeError(
                        "OpenCode returned a matching execution session without an id"
                    )
                if recovered_id not in session_ids:
                    session_ids.append(recovered_id)

        for current_session_id in session_ids:
            if not _renew_before_cleanup_side_effect(manager, lease):
                return "lost"
            try:
                if status == "queued":
                    # A queued row can already have an accepted prompt whose DB
                    # transition was interrupted. Abort first, remove its durable
                    # session, then abort once more so a prompt that crossed the
                    # delete boundary cannot retain an in-memory runner.
                    client.abort(current_session_id)
                    if not _renew_before_cleanup_side_effect(manager, lease):
                        return "lost"
                    client.delete_session(current_session_id)
                    if not _renew_before_cleanup_side_effect(manager, lease):
                        return "lost"
                    client.abort(current_session_id)
                else:
                    client.abort(current_session_id)
            except OpenCodeNotFound:
                pass
    except OpenCodeError as exc:
        LOGGER.warning(
            "Cancellation cleanup pending execution=%s generation=%s: %s",
            lease.execution_id,
            lease.generation,
            exc,
        )
        with SessionLocal() as db:
            return manager.mark_cancel_pending(db, lease, str(exc))

    with SessionLocal() as db:
        return manager.mark_cancelled(
            db,
            lease,
            cleaned_session_ids=session_ids,
        )


def timeout_execution(
    manager: ExecutionLeaseManager,
    client: OpenCodeClient,
    lease: ExecutionLease,
) -> str:
    """Abort every reconciled OpenCode session before committing a timeout.

    A failed or ambiguous abort leaves the execution active in
    ``timeout_abort_pending``. That is deliberate: the database must not claim a
    terminal timeout while external work may still be running.
    """
    with SessionLocal() as db:
        now = utc_now()
        run = manager._locked_owned_run(db, lease, now)
        if run is None:
            db.rollback()
            return "lost"
        session_id = run.opencode_session_id
        db.rollback()

    try:
        session_ids: list[str] = []
        if session_id:
            session_ids.append(session_id)
        else:
            for session in client.sessions_for_execution(lease.execution_id):
                recovered_id = str(session.get("id") or session.get("sessionID") or "")
                if not recovered_id:
                    raise OpenCodeError(
                        "OpenCode returned a matching execution session without an id"
                    )
                if recovered_id not in session_ids:
                    session_ids.append(recovered_id)

        for current_session_id in session_ids:
            if not _renew_before_external_side_effect(manager, lease):
                return "lost"
            try:
                client.abort(current_session_id)
            except OpenCodeNotFound:
                # A missing session cannot continue executing, so 404 is a
                # confirmed terminal state for timeout cleanup.
                pass
    except OpenCodeError as exc:
        LOGGER.warning(
            "Timeout abort pending execution=%s generation=%s: %s",
            lease.execution_id,
            lease.generation,
            exc,
        )
        with SessionLocal() as db:
            return manager.mark_timeout_pending(db, lease, str(exc))

    with SessionLocal() as db:
        return manager.mark_timed_out(
            db,
            lease,
            aborted_session_ids=session_ids,
        )


def dispatch_execution(
    manager: ExecutionLeaseManager,
    client: OpenCodeClient,
    lease: ExecutionLease,
) -> str:
    context = _queued_dispatch_context(manager, lease)
    if context is None:
        return "lost"
    session_id, title, prompt, runtime_verified_before = context

    if not _verify_workspace_before_inference(manager, lease):
        return "rejected"

    if not session_id:
        matches = client.sessions_for_execution(lease.execution_id)
        if len(matches) > 1:
            raise OpenCodeError(
                f"Ambiguous OpenCode dispatch recovery for execution {lease.execution_id}: "
                f"{len(matches)} sessions carry the same metadata"
            )
        if matches:
            session = matches[0]
        else:
            # OpenCode HTTP requests time out after 30s. Production enforces a
            # >=60s lease, and this heartbeat occurs immediately before the POST,
            # so another generation cannot legitimately recover while this side
            # effect is still in flight.
            if not _renew_before_external_side_effect(manager, lease):
                return "lost"
            session = client.create_session(
                title,
                metadata={EXECUTION_METADATA_KEY: lease.execution_id},
            )
        session_id = str(session.get("id") or session.get("sessionID") or "")
        if not session_id:
            raise OpenCodeError("OpenCode не вернул session id")
        with SessionLocal() as db:
            if not manager.persist_dispatch_session(db, lease, session_id):
                return "lost"

    if _deadline_elapsed(lease):
        return timeout_execution(manager, client, lease)

    message_id = execution_message_id(lease.execution_id)
    part_id = execution_part_id(lease.execution_id)
    existing_message = client.message(session_id, message_id)
    if existing_message is not None and lease.workspace_binding is not None:
        if not runtime_verified_before:
            with SessionLocal() as db:
                if not manager.request_safety_cancel(db, lease):
                    return "lost"
            return cancel_execution(manager, client, lease)
    if existing_message is None:
        # A session POST can take time. Re-check the filesystem immediately
        # before the only call that starts inference.
        if not _verify_workspace_before_inference(manager, lease):
            return "rejected"
        if not _renew_before_external_side_effect(manager, lease):
            return "lost"
        client.prompt_async(
            session_id,
            prompt,
            message_id=message_id,
            part_id=part_id,
        )

    if _deadline_elapsed(lease):
        return timeout_execution(manager, client, lease)

    with SessionLocal() as db:
        if not manager.mark_dispatched(db, lease, session_id, message_id, part_id):
            return "lost"
    return "dispatched"


def poll_execution(
    manager: ExecutionLeaseManager,
    client_factory: Callable[[], OpenCodeClient],
    lease: ExecutionLease,
) -> str:
    try:
        client = client_factory()
        if lease.workspace_path is not None:
            client = client.for_directory(lease.workspace_path)
        if lease.cancel_requested_at is not None:
            return cancel_execution(manager, client, lease)
        if _deadline_elapsed(lease):
            return timeout_execution(manager, client, lease)
        if lease.status == "queued":
            return dispatch_execution(manager, client, lease)
        if not lease.opencode_session_id:
            raise OpenCodeError(
                f"Running execution {lease.execution_id} has no OpenCode session id"
            )
        statuses = client.session_statuses()
        messages = client.messages(lease.opencode_session_id)
        state = statuses.get(lease.opencode_session_id) or {}
        state_type = state.get("type") if isinstance(state, dict) else str(state)
        result = extract_last_assistant_text(messages)
        if _deadline_elapsed(lease):
            return timeout_execution(manager, client, lease)
    except OpenCodeError as exc:
        LOGGER.warning(
            "OpenCode operation failed execution=%s generation=%s status=%s: %s",
            lease.execution_id,
            lease.generation,
            lease.status,
            exc,
        )
        with SessionLocal() as db:
            return "running" if manager.heartbeat(db, lease) else "lost"

    with SessionLocal() as db:
        outcome = manager.apply_observation(
            db,
            lease,
            state_type=state_type or "unknown",
            result=result,
        )
    if outcome == "cancel_requested":
        return cancel_execution(manager, client, lease)
    if outcome == "deadline":
        return timeout_execution(manager, client, lease)
    if outcome == "lost":
        LOGGER.warning(
            "Rejected stale execution observation execution=%s generation=%s",
            lease.execution_id,
            lease.generation,
        )
    return outcome


def write_worker_health(path: Path) -> None:
    path.touch(exist_ok=True)


def run_forever(
    manager: ExecutionLeaseManager,
    client_factory: Callable[[], OpenCodeClient],
    *,
    poll_seconds: float,
    max_active: int,
    health_path: Path | None = None,
) -> None:
    active: dict[str, ExecutionLease] = {}
    with ThreadPoolExecutor(
        max_workers=max_active,
        thread_name_prefix="execution-poll",
    ) as pool:
        while True:
            if health_path is not None:
                write_worker_health(health_path)
            slots = max_active - len(active)
            if slots > 0:
                with SessionLocal() as db:
                    claimed = manager.claim_available(db, limit=slots)
                for lease in claimed:
                    active[lease.execution_id] = lease

            futures = {
                pool.submit(poll_execution, manager, client_factory, lease): lease
                for lease in list(active.values())
            }
            for future in as_completed(futures):
                lease = futures[future]
                try:
                    outcome = future.result()
                except Exception:
                    # Worker boundary: never let an unexpected process-level error
                    # commit stale state; the lease expires and another generation recovers.
                    LOGGER.exception(
                        "Unexpected worker failure execution=%s generation=%s",
                        lease.execution_id,
                        lease.generation,
                    )
                    active.pop(lease.execution_id, None)
                    continue
                if outcome != "running":
                    active.pop(lease.execution_id, None)

            time.sleep(poll_seconds)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    worker_id = f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid4().hex[:12]}"
    lease_seconds = _positive_int(
        "CONTROL_PLANE_EXECUTION_WORKER_LEASE_SECONDS",
        120,
        minimum=60,
        maximum=3600,
    )
    max_active = _positive_int(
        "CONTROL_PLANE_EXECUTION_WORKER_MAX_ACTIVE",
        4,
        minimum=1,
        maximum=32,
    )
    poll_seconds = _positive_float(
        "CONTROL_PLANE_EXECUTION_WORKER_POLL_SECONDS",
        5.0,
        minimum=0.5,
    )
    if poll_seconds > 30:
        raise RuntimeError("CONTROL_PLANE_EXECUTION_WORKER_POLL_SECONDS must be <= 30")
    health_path = Path(
        os.getenv(
            "CONTROL_PLANE_EXECUTION_WORKER_HEALTH_PATH",
            "/tmp/ai-orchestra-execution-worker.heartbeat",
        )
    )
    workspace_root = Path(
        os.getenv(
            "CONTROL_PLANE_TASK_WORKSPACE_ROOT",
            str(DEFAULT_OPENCODE_WORKSPACE_ROOT),
        )
    )
    workspace_max_files = _positive_int(
        "CONTROL_PLANE_WORKSPACE_MANAGER_MAX_FILES",
        100_000,
        minimum=1,
        maximum=1_000_000,
    )

    with SessionLocal() as db:
        assert_database_shape(db.get_bind())

    manager = ExecutionLeaseManager(
        worker_id,
        lease_seconds=lease_seconds,
        execution_timeout_seconds=settings.execution_timeout_seconds,
        workspace_root=workspace_root,
        workspace_max_files=workspace_max_files,
    )

    def client_factory() -> OpenCodeClient:
        return OpenCodeClient(
            settings.opencode_internal_url,
            settings.opencode_username,
            settings.opencode_password,
        )

    LOGGER.info(
        "Execution worker started worker_id=%s lease_seconds=%s max_active=%s poll_seconds=%s",
        worker_id,
        lease_seconds,
        max_active,
        poll_seconds,
    )
    run_forever(
        manager,
        client_factory,
        poll_seconds=poll_seconds,
        max_active=max_active,
        health_path=health_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
