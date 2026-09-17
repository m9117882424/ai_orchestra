from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from .evidence import capture_opencode_evidence, record_evidence
from .models import ExecutionChildRun, ExecutionRun, UsageEvent

REVIEW_ROLES = frozenset({"qa-engineer", "code-reviewer", "trade-reviewer"})
TERMINAL_CHILD_STATUSES = frozenset({"completed", "failed"})
VERDICT_RE = re.compile(
    r"<AI_ORCHESTRA_VERDICT>\s*(\{.*?\})\s*</AI_ORCHESTRA_VERDICT>",
    re.DOTALL,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: object, *, fallback: datetime | None = None) -> datetime | None:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return fallback
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return fallback
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return fallback


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bounded_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def _model_identity(session: dict | None, state: dict) -> tuple[str | None, str | None]:
    model = (session or {}).get("model")
    if not isinstance(model, dict):
        metadata = state.get("metadata") or {}
        model = metadata.get("model") if isinstance(metadata, dict) else None
    if not isinstance(model, dict):
        return None, None
    provider = model.get("providerID") or model.get("provider")
    model_id = model.get("id") or model.get("modelID") or model.get("model")
    return (
        _bounded_text(provider, 80),
        _bounded_text(model_id, 120),
    )


def _child_status(value: object) -> str:
    raw = str(value or "unknown").lower()
    if raw in {"pending", "queued"}:
        return "pending"
    if raw in {"running", "busy"}:
        return "running"
    if raw in {"completed", "complete", "success", "succeeded"}:
        return "completed"
    if raw in {"error", "failed", "cancelled", "canceled", "timed_out"}:
        return "failed"
    return "unknown"


def _safe_nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _safe_cost(value: object) -> Decimal:
    if isinstance(value, bool):
        return Decimal("0")
    try:
        result = Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    if result < 0:
        return Decimal("0")
    return result.quantize(Decimal("0.000001"))


def _upsert_usage_snapshot(
    db: Session,
    *,
    run: ExecutionRun,
    session: dict,
    child_run_id: str | None,
    fallback_role: str,
) -> bool:
    source_run_id = _bounded_text(session.get("id"), 160)
    if source_run_id is None:
        return False
    model = session.get("model") if isinstance(session.get("model"), dict) else {}
    role = _bounded_text(session.get("agent"), 80) or fallback_role[:80]
    provider = _bounded_text(model.get("providerID") or model.get("provider"), 80) or "unknown"
    model_id = _bounded_text(model.get("id") or model.get("modelID"), 120) or "unknown"
    tokens = session.get("tokens") if isinstance(session.get("tokens"), dict) else {}
    input_tokens = _safe_nonnegative_int(tokens.get("input"))
    output_tokens = _safe_nonnegative_int(tokens.get("output"))
    cost = _safe_cost(session.get("cost"))
    existing = db.scalar(
        select(UsageEvent).where(
            UsageEvent.execution_id == run.id,
            UsageEvent.source == "opencode-session",
            UsageEvent.source_key == source_run_id,
        )
    )
    if existing is None:
        db.add(
            UsageEvent(
                task_id=run.task_id,
                execution_id=run.id,
                source="opencode-session",
                source_key=source_run_id,
                child_run_id=child_run_id,
                role=role,
                provider=provider,
                model=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=cost,
            )
        )
        db.flush()
        return True
    existing.child_run_id = child_run_id
    existing.role = role
    existing.provider = provider
    existing.model = model_id
    existing.input_tokens = input_tokens
    existing.output_tokens = output_tokens
    existing.cost = cost
    db.flush()
    return True


def _verdict_payload(output: object) -> dict | None:
    if not isinstance(output, str):
        return None
    matches = VERDICT_RE.findall(output)
    if not matches:
        return None
    try:
        payload = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    verdict = str(payload.get("verdict") or "").lower()
    if verdict not in {"pass", "fail", "blocked"}:
        return None
    summary = _bounded_text(payload.get("summary"), 2000)
    if summary is None:
        return None
    findings: list[dict] = []
    raw_findings = payload.get("findings")
    if isinstance(raw_findings, list):
        for item in raw_findings[:50]:
            if not isinstance(item, dict):
                continue
            text = _bounded_text(item.get("summary"), 1000)
            if text is None:
                continue
            severity = str(item.get("severity") or "info").lower()
            if severity not in {"critical", "high", "medium", "low", "info"}:
                severity = "info"
            finding = {"severity": severity, "summary": text}
            path = _bounded_text(item.get("path"), 512)
            if path:
                finding["path"] = path
            findings.append(finding)
    return {"version": 1, "verdict": verdict, "summary": summary, "findings": findings}


def _task_fingerprint(role: str, title: object) -> str | None:
    text = _bounded_text(title, 2000)
    if text is None or role == "unknown":
        return None
    return hashlib.sha256(f"{role}\0{text}".encode("utf-8")).hexdigest()


def _upsert_child_run(
    db: Session,
    *,
    run: ExecutionRun,
    root_session_id: str,
    part: dict,
    session: dict | None,
    observed_at: datetime,
) -> tuple[ExecutionChildRun | None, bool]:
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    child_session_id = _bounded_text(metadata.get("sessionId"), 160)
    if child_session_id is None:
        return None, False
    role = _bounded_text((session or {}).get("agent"), 80) or "unknown"
    provider, model = _model_identity(session, state)
    status = _child_status(state.get("status"))
    time_info = state.get("time") if isinstance(state.get("time"), dict) else {}
    session_time = (session or {}).get("time") if isinstance((session or {}).get("time"), dict) else {}
    started_at = _timestamp(time_info.get("start")) or _timestamp(session_time.get("created")) or observed_at
    finished_at = None
    if status in TERMINAL_CHILD_STATUSES:
        finished_at = _timestamp(time_info.get("end")) or _timestamp(session_time.get("updated")) or observed_at
    fingerprint = _task_fingerprint(role, state.get("title"))
    existing = db.scalar(
        select(ExecutionChildRun).where(
            ExecutionChildRun.execution_id == run.id,
            ExecutionChildRun.source == "opencode",
            ExecutionChildRun.source_run_id == child_session_id,
        )
    )
    created = existing is None
    if existing is None:
        retry_of = None
        attempt = 1
        if fingerprint is not None:
            retry_of = db.scalar(
                select(ExecutionChildRun)
                .where(
                    ExecutionChildRun.execution_id == run.id,
                    ExecutionChildRun.role == role,
                    ExecutionChildRun.task_fingerprint == fingerprint,
                    ExecutionChildRun.status == "failed",
                    ExecutionChildRun.started_at < started_at,
                )
                .order_by(ExecutionChildRun.started_at.desc(), ExecutionChildRun.id.desc())
                .limit(1)
            )
            if retry_of is not None:
                attempt = retry_of.attempt + 1
        existing = ExecutionChildRun(
            execution_id=run.id,
            source="opencode",
            source_run_id=child_session_id,
            parent_source_run_id=root_session_id[:160],
            parent_call_id=_bounded_text(part.get("callID") or part.get("id"), 160),
            role=role,
            provider=provider,
            model=model,
            status=status,
            task_fingerprint=fingerprint,
            attempt=attempt,
            retry_of_id=retry_of.id if retry_of is not None else None,
            started_at=started_at,
            finished_at=finished_at,
            last_observed_at=observed_at,
            created_at=observed_at,
            updated_at=observed_at,
        )
        db.add(existing)
    else:
        existing.parent_source_run_id = root_session_id[:160]
        existing.parent_call_id = _bounded_text(part.get("callID") or part.get("id"), 160)
        existing.role = role if role != "unknown" else existing.role
        existing.provider = provider or existing.provider
        existing.model = model or existing.model
        if existing.status not in TERMINAL_CHILD_STATUSES or status in TERMINAL_CHILD_STATUSES:
            existing.status = status
        existing.task_fingerprint = fingerprint or existing.task_fingerprint
        existing.started_at = min(_as_utc(existing.started_at), _as_utc(started_at))
        existing.finished_at = finished_at or existing.finished_at
        existing.last_observed_at = observed_at
        existing.updated_at = observed_at
    db.flush()
    return existing, created


def capture_opencode_observability(
    db: Session,
    *,
    execution_id: str,
    generation: int,
    root_session_id: str,
    session_state: str,
    messages: list[dict],
    sessions: list[dict],
    observed_at: datetime | None = None,
) -> int:
    """Persist G4.1 evidence plus G4.2 child-run and automatic usage telemetry.

    Raw task inputs and outputs are never stored. A task output is inspected only
    for the bounded AI_ORCHESTRA_VERDICT JSON contract and then discarded.
    """
    observed_at = observed_at or utc_now()
    created = capture_opencode_evidence(
        db,
        execution_id=execution_id,
        generation=generation,
        session_state=session_state,
        messages=messages,
    )
    run = db.get(ExecutionRun, execution_id)
    if run is None or run.status != "running" or run.lease_generation != generation:
        return created

    by_id = {
        str(item.get("id")): item
        for item in sessions
        if isinstance(item, dict) and item.get("id")
    }
    root_session = by_id.get(root_session_id)
    if root_session is not None:
        created += int(
            _upsert_usage_snapshot(
                db,
                run=run,
                session=root_session,
                child_run_id=None,
                fallback_role=run.lead_role,
            )
        )

    for message in messages:
        for part in message.get("parts") or []:
            if not isinstance(part, dict) or part.get("type") != "tool" or part.get("tool") != "task":
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
            child_session_id = _bounded_text(metadata.get("sessionId"), 160)
            if child_session_id is None:
                continue
            child_session = by_id.get(child_session_id)
            child, was_created = _upsert_child_run(
                db,
                run=run,
                root_session_id=root_session_id,
                part=part,
                session=child_session,
                observed_at=observed_at,
            )
            if child is None:
                continue
            created += int(was_created)
            if child_session is not None:
                created += int(
                    _upsert_usage_snapshot(
                        db,
                        run=run,
                        session=child_session,
                        child_run_id=child.id,
                        fallback_role=child.role,
                    )
                )
            if child.role in REVIEW_ROLES and child.status in TERMINAL_CHILD_STATUSES:
                verdict = _verdict_payload(state.get("output"))
                if verdict is not None:
                    # record_evidence provides source-key idempotency; raw output is discarded.
                    record_evidence(
                        db,
                        execution_id=run.id,
                        source="opencode",
                        source_key=f"verdict:{child.source_run_id}",
                        kind="review",
                        role=child.role,
                        model=child.model,
                        status=verdict["verdict"],
                        attempt=child.attempt,
                        details={
                            "child_run_id": child.id,
                            "source_run_id": child.source_run_id,
                            **verdict,
                        },
                        occurred_at=child.finished_at or observed_at,
                    )
                    created += 1
    return created
