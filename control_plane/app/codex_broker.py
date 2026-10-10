"""Narrow, token-authenticated subscription worker protocol.

The token grants access only to this router. It never grants manager API access.
Running leases are never reclaimed: a lost worker may still have a model process.
"""
from __future__ import annotations

import re
import secrets
import stat
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .evidence import materialize_terminal_result_package
from .models import ExecutionChildRun, ExecutionEvidence, ExecutionRun, Repository, Task, TaskWorkspace
from .services import write_audit
from .settings import get_settings
from .workspace_manager import request_workspace_inspection
from .workspace_protocol import WorkspaceBinding, WorkspacePreflightError, workspace_path_for

router = APIRouter(prefix="/api/codex-worker", tags=["codex-worker"])
ROLES = ("architect", "qa", "reviewer")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
Db = Annotated[Session, Depends(get_db)]


def now_utc():
    return datetime.now(timezone.utc)


def as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def require_worker(authorization: Annotated[str | None, Header()] = None):
    filename = get_settings().codex_worker_token_file
    if not filename:
        raise HTTPException(503, "Subscription worker disabled")
    try:
        path = Path(filename)
        if not path.is_absolute():
            raise OSError("token file path")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
                raise OSError("token file permissions")
            token = os.read(fd, 4096).decode("ascii").strip()
        finally:
            os.close(fd)
        if len(token) < 32 or not token.isascii():
            raise OSError("token file content")
    except (OSError, UnicodeError):
        raise HTTPException(503, "Subscription worker unavailable") from None
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not authorization or not authorization.startswith("Bearer ") or not secrets.compare_digest(supplied, token):
        raise HTTPException(401, "Worker authentication failed")


Worker = Annotated[None, Depends(require_worker)]


def binding(db: Session, run: ExecutionRun) -> WorkspaceBinding:
    workspace = db.get(TaskWorkspace, run.workspace_id)
    if not workspace or workspace.status != "ready" or any((
        run.contract_version != 2,
        workspace.version < 1,
        workspace.task_id != run.task_id,
        workspace.repository_id != run.repository_id,
        workspace.base_commit != run.base_commit,
        workspace.opencode_path != run.workspace_path,
        workspace.initial_tree != run.workspace_tree,
        workspace.preflight_digest != run.workspace_preflight_digest,
        not isinstance(workspace.preflight_digest, str) or HEX64.fullmatch(workspace.preflight_digest) is None,
        not isinstance(workspace.initial_tree, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", workspace.initial_tree) is None,
        not isinstance(workspace.base_commit, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", workspace.base_commit) is None,
        not isinstance(workspace.tracked_entries, int) or workspace.tracked_entries < 0,
        workspace.opencode_path != workspace_path_for(workspace.id),
        run.workspace_preflight_completed_at is None,
    )):
        raise WorkspacePreflightError("workspace_database_binding_mismatch")
    return WorkspaceBinding(
        execution_id=run.id, task_id=run.task_id,
        repository_id=workspace.repository_id, workspace_id=workspace.id,
        base_commit=workspace.base_commit, branch_name=workspace.branch_name,
        workspace_path=workspace.opencode_path, initial_tree=workspace.initial_tree,
        tracked_entries=workspace.tracked_entries,
        preflight_digest=workspace.preflight_digest,
    )


def repository_trusted(db: Session, run: ExecutionRun) -> bool:
    repository = db.get(Repository, run.repository_id)
    return bool(repository and repository.enabled and repository.status == "ready"
                and repository.last_known_commit == run.base_commit
                and repository.assurance_tier == "general-standard")


def terminal(db: Session, run: ExecutionRun, code: str, now: datetime, *, status: str = "failed"):
    run.status = status
    run.stage = code
    run.error = code
    run.finished_at = now
    run.heartbeat_at = now
    run.lease_generation += 1
    run.lease_owner = None
    run.lease_expires_at = None
    run.updated_at = now
    task = db.get(Task, run.task_id)
    if task and task.status in {"in_progress", "qa", "waiting_approval"}:
        task.status = "failed"
        task.updated_at = now
    workspace = db.get(TaskWorkspace, run.workspace_id) if run.workspace_id else None
    if workspace is not None and workspace.status == "ready":
        request_workspace_inspection(db, run, actor="codex-worker", now=now)
    write_audit(db, actor="codex-worker", action=f"execution.{status}", entity_type="execution", entity_id=run.id, details={"code": code})
    materialize_terminal_result_package(db, run, now=now)


def owned(db: Session, run_id: str, generation: int) -> ExecutionRun:
    run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update())
    now = now_utc()
    if not run or run.backend != "codex-subscription" or run.status != "running" or run.lease_generation != generation or run.lease_owner != "codex-worker":
        raise HTTPException(409, "Lease lost")
    if run.cancel_requested_at or as_utc(run.deadline_at) <= now or as_utc(run.lease_expires_at) <= now:
        terminal(db, run, "codex_cancelled" if run.cancel_requested_at else "codex_lease_expired",
                 now, status="cancelled" if run.cancel_requested_at else "failed")
        db.commit()
        raise HTTPException(409, "Lease expired")
    return run


@router.post("/claim")
def claim(db: Db, _: Worker):
    now = now_utc()
    # Never recover a running inference. An expired worker is fenced and failed.
    expired = list(db.scalars(select(ExecutionRun).where(
        ExecutionRun.backend == "codex-subscription", ExecutionRun.status == "running",
        ExecutionRun.lease_expires_at <= now,
    ).with_for_update(skip_locked=True)))
    for run in expired:
        terminal(db, run, "codex_cancelled" if run.cancel_requested_at else "codex_lease_expired",
                 now, status="cancelled" if run.cancel_requested_at else "failed")
    queued = db.scalar(select(ExecutionRun).where(
        ExecutionRun.backend == "codex-subscription", ExecutionRun.status == "queued",
    ).order_by(ExecutionRun.created_at).with_for_update(skip_locked=True))
    if queued is None:
        db.commit()
        return None
    if queued.cancel_requested_at or not queued.deadline_at or as_utc(queued.deadline_at) <= now:
        terminal(db, queued, "codex_cancelled" if queued.cancel_requested_at else "codex_deadline_expired",
                 now, status="cancelled" if queued.cancel_requested_at else "failed")
        db.commit()
        return None
    if not repository_trusted(db, queued):
        terminal(db, queued, "repository_trust_revoked", now)
        db.commit()
        return None
    try:
        workspace = binding(db, queued)
    except (WorkspacePreflightError, OSError) as exc:
        terminal(db, queued, getattr(exc, "code", "workspace_runtime_unavailable"), now)
        db.commit()
        return None
    task = db.get(Task, queued.task_id)
    if not task:
        terminal(db, queued, "task_missing", now)
        db.commit()
        return None
    queued.lease_generation += 1
    queued.lease_owner = "codex-worker"
    queued.lease_expires_at = now + timedelta(seconds=120)
    queued.heartbeat_at = now
    queued.status = "running"
    queued.stage = "architect_running"
    queued.updated_at = now
    write_audit(db, actor="codex-worker", action="execution.claimed", entity_type="execution", entity_id=queued.id, details={"generation": queued.lease_generation, "backend": queued.backend})
    response = {"execution_id": queued.id, "generation": queued.lease_generation,
                "task_id": task.id, "title": task.title, "description": task.description,
                "workspace": workspace.__dict__, "deadline_at": queued.deadline_at.isoformat()}
    db.commit()
    return response


class LeaseRequest(BaseModel):
    generation: int = Field(ge=1)


@router.post("/{run_id}/heartbeat")
def heartbeat(run_id: str, body: LeaseRequest, db: Db, _: Worker):
    run = owned(db, run_id, body.generation)
    now = now_utc()
    run.heartbeat_at = now
    run.lease_expires_at = now + timedelta(seconds=120)
    run.updated_at = now
    db.commit()
    return {"ok": True, "stage": run.stage}


class PhaseResult(LeaseRequest):
    model_config = ConfigDict(extra="forbid")
    role: str
    outcome: str
    model: str = Field(max_length=120)
    final_text: str = Field(max_length=524288)
    final_sha256: str
    events_sha256: str
    artifact_path: str = Field(max_length=512)
    base_commit: str
    base_tree: str
    workspace_preflight_digest: str


@router.post("/{run_id}/phase")
def phase(run_id: str, body: PhaseResult, db: Db, _: Worker):
    run = owned(db, run_id, body.generation)
    expected = next((role for role in ROLES if run.stage == f"{role}_running"), None)
    if expected is None or body.role != expected:
        raise HTTPException(409, "Unexpected role transition")
    if body.outcome not in {"completed", "execution_failed", "local_timeout", "incomplete_result", "read_only_violation", "auth_failed", "workspace_failed"}:
        raise HTTPException(422, "Invalid outcome")
    if body.base_commit != run.base_commit or body.base_tree != run.workspace_tree:
        raise HTTPException(409, "Immutable base mismatch")
    if body.workspace_preflight_digest != run.workspace_preflight_digest:
        raise HTTPException(409, "Workspace attestation mismatch")
    try:
        workspace = binding(db, run)
    except WorkspacePreflightError:
        raise HTTPException(409, "Workspace binding changed") from None
    if body.workspace_preflight_digest != workspace.preflight_digest:
        raise HTTPException(409, "Workspace attestation mismatch")
    if len(body.final_text.encode("utf-8")) > 1048576:
        raise HTTPException(422, "Final report too large")
    if not all(HEX64.fullmatch(value) for value in (body.final_sha256, body.events_sha256)):
        raise HTTPException(422, "Invalid evidence checksum")
    if body.outcome == "completed" and (not body.final_text.strip() or not body.model.strip()):
        raise HTTPException(422, "Missing result")
    import hashlib
    if body.outcome == "completed" and hashlib.sha256(body.final_text.encode("utf-8")).hexdigest() != body.final_sha256:
        raise HTTPException(422, "Final report checksum mismatch")
    now = now_utc()
    if not repository_trusted(db, run):
        terminal(db, run, "repository_trust_revoked", now)
        db.commit()
        return {"status": "failed"}
    if body.outcome == "completed":
        run.workspace_runtime_preflight_digest = body.workspace_preflight_digest
        run.workspace_runtime_verified_at = now
    child = ExecutionChildRun(
        execution_id=run.id, source="codex-subscription",
        source_run_id=f"{run.id}:{body.role}", role=body.role,
        provider="chatgpt-subscription", model=body.model,
        status="completed" if body.outcome == "completed" else "failed",
        attempt=1, started_at=run.heartbeat_at or now, finished_at=now,
        last_observed_at=now,
    )
    db.add(child)
    db.add(ExecutionEvidence(
        execution_id=run.id, source="codex-subscription",
        source_key=f"{run.id}:{body.role}:artifact",
        kind="artifact" if body.role == "architect" else "review", role=body.role,
        model=body.model, status=body.outcome,
        details={"path": body.artifact_path, "final_sha256": body.final_sha256,
                 "events_sha256": body.events_sha256, "base_commit": body.base_commit,
                 "base_tree": body.base_tree, "usage": "subscription_unknown",
                 "api_cost": "unknown", "final_text": body.final_text}, occurred_at=now,
    ))
    db.flush()
    if body.outcome != "completed":
        terminal(db, run, f"codex_{body.role}_failed", now)
    elif body.role == "reviewer":
        run.status = "completed"
        run.stage = "reviewer_completed"
        run.result = body.final_text
        run.finished_at = now
        run.lease_owner = None
        run.lease_expires_at = None
        task = db.get(Task, run.task_id)
        if task and task.status == "in_progress":
            task.status = "qa"
            task.updated_at = now
        request_workspace_inspection(db, run, actor="codex-worker", now=now)
        materialize_terminal_result_package(db, run, now=now)
    else:
        next_role = ROLES[ROLES.index(body.role) + 1]
        run.stage = f"{next_role}_running"
        run.heartbeat_at = now
        run.lease_expires_at = now + timedelta(seconds=120)
    run.updated_at = now
    write_audit(db, actor="codex-worker", action="execution.role_finished", entity_type="execution", entity_id=run.id, details={"role": body.role, "outcome": body.outcome, "generation": body.generation})
    db.commit()
    return {"status": run.status, "stage": run.stage}
