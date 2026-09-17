from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    ExecutionChildRun,
    ExecutionEvidence,
    ExecutionResultPackage,
    ExecutionRun,
    RunnerJob,
    Task,
    TaskWorkspace,
    UsageEvent,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized else None


def _message_time(info: dict) -> datetime:
    raw = (info.get("time") or {}).get("created") or info.get("createdAt")
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000 if raw > 10_000_000_000 else raw, tz=timezone.utc)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return _as_utc(parsed) or utc_now()
        except ValueError:
            pass
    return utc_now()


def record_evidence(
    db: Session,
    *,
    execution_id: str,
    source: str,
    source_key: str,
    kind: str,
    role: str | None = None,
    model: str | None = None,
    tool_name: str | None = None,
    status: str | None = None,
    attempt: int = 1,
    retry_of_id: str | None = None,
    details: dict | None = None,
    occurred_at: datetime | None = None,
) -> ExecutionEvidence:
    existing = db.scalar(
        select(ExecutionEvidence).where(
            ExecutionEvidence.execution_id == execution_id,
            ExecutionEvidence.source == source,
            ExecutionEvidence.source_key == source_key,
        )
    )
    if existing is not None:
        return existing
    event = ExecutionEvidence(
        execution_id=execution_id,
        source=source[:40],
        source_key=source_key[:160],
        kind=kind,
        role=role[:80] if role else None,
        model=model[:120] if model else None,
        tool_name=tool_name[:120] if tool_name else None,
        status=status[:40] if status else None,
        attempt=max(1, int(attempt)),
        retry_of_id=retry_of_id,
        details=details or {},
        occurred_at=_as_utc(occurred_at) or utc_now(),
    )
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(ExecutionEvidence).where(
                ExecutionEvidence.execution_id == execution_id,
                ExecutionEvidence.source == source,
                ExecutionEvidence.source_key == source_key,
            )
        )
        if existing is None:
            raise
        return existing
    return event


def capture_opencode_evidence(
    db: Session,
    *,
    execution_id: str,
    generation: int,
    session_state: str,
    messages: list[dict],
) -> int:
    run = db.get(ExecutionRun, execution_id)
    if run is None or run.status != "running" or run.lease_generation != generation:
        return 0

    created = 0
    state_key = f"session-state:{generation}:{session_state}"
    if db.scalar(
        select(ExecutionEvidence.id).where(
            ExecutionEvidence.execution_id == execution_id,
            ExecutionEvidence.source == "opencode",
            ExecutionEvidence.source_key == state_key,
        )
    ) is None:
        record_evidence(
            db,
            execution_id=execution_id,
            source="opencode",
            source_key=state_key,
            kind="stage",
            status=session_state or "unknown",
            attempt=max(1, generation),
            details={"lease_generation": generation},
        )
        created += 1

    for item in messages:
        info = item.get("info") or {}
        message_id = info.get("id")
        if not isinstance(message_id, str) or not message_id:
            continue
        role = str(info.get("agent") or info.get("role") or "assistant")
        model = info.get("model") or info.get("modelID")
        occurred_at = _message_time(info)
        text_parts = [
            str(part.get("text") or "").strip()
            for part in (item.get("parts") or [])
            if part.get("type") == "text" and not part.get("progress_only") and part.get("text")
        ]
        text_value = "\n".join(part for part in text_parts if part).strip()
        message_key = f"message:{message_id}"
        if text_value and db.scalar(
            select(ExecutionEvidence.id).where(
                ExecutionEvidence.execution_id == execution_id,
                ExecutionEvidence.source == "opencode",
                ExecutionEvidence.source_key == message_key,
            )
        ) is None:
            record_evidence(
                db,
                execution_id=execution_id,
                source="opencode",
                source_key=message_key,
                kind="message",
                role=role,
                model=str(model) if model else None,
                status=str(info.get("finish") or "observed"),
                attempt=max(1, generation),
                details={"text": text_value[:4000], "message_id": message_id},
                occurred_at=occurred_at,
            )
            created += 1

        for index, part in enumerate(item.get("parts") or []):
            if part.get("type") != "tool":
                continue
            state = part.get("state") or {}
            call_id = part.get("callID") or part.get("id") or f"{message_id}:{index}"
            tool_name = str(part.get("tool") or "tool")
            tool_status = str(state.get("status") or "unknown")
            tool_key = f"tool:{call_id}:{tool_status}"
            if db.scalar(
                select(ExecutionEvidence.id).where(
                    ExecutionEvidence.execution_id == execution_id,
                    ExecutionEvidence.source == "opencode",
                    ExecutionEvidence.source_key == tool_key,
                )
            ) is not None:
                continue
            tool_time = occurred_at
            raw_start = (state.get("time") or {}).get("start")
            if isinstance(raw_start, (int, float)):
                tool_time = datetime.fromtimestamp(
                    raw_start / 1000 if raw_start > 10_000_000_000 else raw_start,
                    tz=timezone.utc,
                )
            record_evidence(
                db,
                execution_id=execution_id,
                source="opencode",
                source_key=tool_key,
                kind="tool",
                role=role,
                model=str(model) if model else None,
                tool_name=tool_name,
                status=tool_status,
                attempt=max(1, generation),
                details={"call_id": str(call_id), "message_id": message_id},
                occurred_at=tool_time,
            )
            created += 1
    return created


def execution_cost_summary(db: Session, execution_id: str) -> dict:
    row = db.execute(
        select(
            func.coalesce(func.sum(UsageEvent.input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.output_tokens), 0),
            func.coalesce(func.sum(UsageEvent.cost), Decimal("0")),
        ).where(UsageEvent.execution_id == execution_id)
    ).one()
    known_cost = Decimal(row[2] or Decimal("0")).quantize(Decimal("0.000001"))
    unknown_automatic_cost_rows = db.scalar(
        select(func.count(UsageEvent.id)).where(
            UsageEvent.execution_id == execution_id,
            UsageEvent.source == "opencode-session",
            UsageEvent.cost == 0,
            (UsageEvent.input_tokens > 0) | (UsageEvent.output_tokens > 0),
        )
    ) or 0
    if unknown_automatic_cost_rows:
        cost_status = "partial" if known_cost > 0 else "unknown"
        actual_cost = None
    else:
        cost_status = "known"
        actual_cost = str(known_cost)
    return {
        "input_tokens": int(row[0] or 0),
        "output_tokens": int(row[1] or 0),
        "actual_cost": actual_cost,
        "known_cost": str(known_cost),
        "cost_status": cost_status,
        "unknown_automatic_cost_rows": int(unknown_automatic_cost_rows),
    }


def build_result_package_payload(db: Session, execution_id: str) -> dict:
    # SessionLocal intentionally disables autoflush. A result package must cover
    # evidence/usage written in the same terminal-state transaction before its
    # content digest is calculated.
    db.flush()
    run = db.get(ExecutionRun, execution_id)
    if run is None:
        raise ValueError("execution_not_found")
    task = db.get(Task, run.task_id)
    workspace = db.get(TaskWorkspace, run.workspace_id) if run.workspace_id else None
    jobs = list(
        db.scalars(
            select(RunnerJob)
            .where(RunnerJob.execution_id == execution_id)
            .order_by(RunnerJob.created_at.asc(), RunnerJob.id.asc())
        )
    )
    evidence = list(
        db.scalars(
            select(ExecutionEvidence)
            .where(ExecutionEvidence.execution_id == execution_id)
            .order_by(ExecutionEvidence.occurred_at.asc(), ExecutionEvidence.id.asc())
        )
    )
    child_runs = list(
        db.scalars(
            select(ExecutionChildRun)
            .where(ExecutionChildRun.execution_id == execution_id)
            .order_by(ExecutionChildRun.started_at.asc(), ExecutionChildRun.id.asc())
        )
    )
    automatic_usage_count = db.scalar(
        select(func.count(UsageEvent.id)).where(
            UsageEvent.execution_id == execution_id,
            UsageEvent.source == "opencode-session",
        )
    ) or 0
    cost_summary = execution_cost_summary(db, execution_id)
    limitations: list[str] = []
    if workspace is not None and workspace.status != "retained":
        limitations.append("workspace inspection is not final")
    if workspace is not None and workspace.changed_file_count:
        limitations.append("changed-file names and full diff are not persisted yet")
    if not any(event.kind == "review" for event in evidence):
        limitations.append("structured QA/reviewer verdicts are not persisted yet")
    if not any(event.kind == "artifact" for event in evidence):
        limitations.append("generated artifact provenance is not persisted yet")
    if automatic_usage_count == 0:
        limitations.append("automatic provider token/cost telemetry was unavailable")
    elif cost_summary["unknown_automatic_cost_rows"]:
        limitations.append("automatic provider monetary cost was unavailable; token telemetry is present")

    return {
        "schema_version": 2,
        "execution_id": run.id,
        "original_task": {
            "id": task.id if task else run.task_id,
            "title": task.title if task else None,
            "description": task.description if task else None,
            "project": task.project if task else None,
            "domain": task.domain if task else None,
            "priority": task.priority if task else None,
            "risk_level": task.risk_level if task else None,
        },
        "plan": None,
        "source": {
            "base_sha": run.base_commit,
            "head_sha": workspace.current_head_commit if workspace else run.base_commit,
            "base_tree": run.workspace_tree,
            "head_tree": workspace.current_tree if workspace else run.workspace_tree,
            "source_snapshot_digests": sorted(
                {job.source_snapshot_digest for job in jobs if job.source_snapshot_digest}
            ),
        },
        "changes": {
            "has_changes": workspace.has_changes if workspace else None,
            "changed_file_count": workspace.changed_file_count if workspace else None,
            "change_digest": workspace.change_digest if workspace else None,
            "changed_files": [],
            "diff": None,
        },
        "commits": [],
        "proposed_commits": [],
        "checks": [
            {
                "job_id": job.id,
                "label": job.checkpoint_label,
                "argv": job.argv,
                "status": job.status,
                "exit_code": job.exit_code,
                "runner_image_id": job.runner_image_id,
                "source_snapshot_digest": job.source_snapshot_digest,
                "checkpoint_digest": job.checkpoint_digest,
                "stdout": (job.stdout or "")[:12000],
                "stderr": (job.stderr or "")[:12000],
                "output_truncated": bool(job.output_truncated),
                "cleanup_confirmed": job.cleanup_confirmed,
                "started_at": _iso(job.started_at),
                "finished_at": _iso(job.finished_at),
            }
            for job in jobs
        ],
        "child_runs": [
            {
                "id": child.id,
                "source": child.source,
                "source_run_id": child.source_run_id,
                "parent_source_run_id": child.parent_source_run_id,
                "parent_call_id": child.parent_call_id,
                "role": child.role,
                "provider": child.provider,
                "model": child.model,
                "status": child.status,
                "attempt": child.attempt,
                "retry_of_id": child.retry_of_id,
                "started_at": _iso(child.started_at),
                "finished_at": _iso(child.finished_at),
                "last_observed_at": _iso(child.last_observed_at),
            }
            for child in child_runs
        ],
        "reviewer_qa_verdicts": [
            {
                "role": event.role,
                "status": event.status,
                "details": event.details,
                "occurred_at": _iso(event.occurred_at),
            }
            for event in evidence
            if event.kind == "review"
        ],
        "generated_artifacts": [event.details for event in evidence if event.kind == "artifact"],
        "execution": {
            "status": run.status,
            "stage": run.stage,
            "lead_role": run.lead_role,
            "assigned_roles": run.assigned_roles,
            "result": run.result,
            "error": run.error,
            "created_at": _iso(run.created_at),
            "finished_at": _iso(run.finished_at),
        },
        "cost": cost_summary,
        "usage_capture": {
            "automatic_session_rows": int(automatic_usage_count),
            "automatic_provider_capture": bool(automatic_usage_count),
            "automatic_cost_capture": bool(automatic_usage_count)
            and cost_summary["unknown_automatic_cost_rows"] == 0,
        },
        "known_risks_limitations": limitations,
    }


def materialize_result_package(
    db: Session,
    execution_id: str,
    *,
    final: bool,
    now: datetime | None = None,
) -> ExecutionResultPackage:
    now = _as_utc(now) or utc_now()
    payload = build_result_package_payload(db, execution_id)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    package = db.get(ExecutionResultPackage, execution_id)
    if package is not None and package.state == "final":
        if package.package_digest != digest:
            raise ValueError("final_result_package_is_immutable")
        return package
    if package is None:
        package = ExecutionResultPackage(
            execution_id=execution_id,
            package_version=2,
            state="final" if final else "provisional",
            payload=payload,
            package_digest=digest,
            generated_at=now,
            finalized_at=now if final else None,
            created_at=now,
            updated_at=now,
        )
        db.add(package)
    else:
        package.package_version = 2
        package.state = "final" if final else "provisional"
        package.payload = payload
        package.package_digest = digest
        package.generated_at = now
        package.finalized_at = now if final else None
        package.updated_at = now
    db.flush()
    return package


def materialize_terminal_result_package(
    db: Session,
    run: ExecutionRun,
    *,
    now: datetime | None = None,
) -> ExecutionResultPackage:
    if run.status not in {"completed", "failed", "cancelled"}:
        raise ValueError("execution_is_not_terminal")
    workspace = db.get(TaskWorkspace, run.workspace_id) if run.workspace_id else None
    inspection_pending = workspace is not None and workspace.status in {
        "inspection_pending",
        "inspecting",
    }
    return materialize_result_package(
        db,
        run.id,
        final=not inspection_pending,
        now=now,
    )
