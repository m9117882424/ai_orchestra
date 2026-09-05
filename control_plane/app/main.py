from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .auth import require_control_request, require_manager
from .db import get_db
from .models import Approval, AuditEvent, Budget, CapabilityGuard, ExecutionRun, Task, UsageEvent
from .schemas import (
    ApprovalCreate,
    ApprovalDecisionRequest,
    ApprovalRead,
    AuditRead,
    BudgetRead,
    BudgetUpdate,
    CapabilityGuardRead,
    ExecutionRead,
    ExecutionProgressRead,
    TaskCreate,
    TaskRead,
    TaskStatusUpdate,
    UsageCreate,
    UsageRead,
)
from .services import current_month_cost, seed_defaults, write_audit
from .opencode_client import OpenCodeClient, OpenCodeError, extract_last_assistant_text
from .settings import get_settings


TASK_TRANSITIONS = {
    "backlog": {"planned", "in_progress"},
    "planned": {"backlog", "in_progress"},
    "in_progress": {"waiting_approval", "qa", "failed"},
    "waiting_approval": {"in_progress", "qa", "failed"},
    "qa": {"done", "in_progress", "failed"},
    "failed": {"planned", "in_progress"},
    "done": set(),
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    from .db import SessionLocal

    with SessionLocal() as db:
        seed_defaults(db, get_settings().default_monthly_budget)
    yield


app = FastAPI(
    title="AI Orchestra Control Plane",
    version="0.4.1",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

base_dir = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(base_dir / "templates"))
app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static")

DbSession = Annotated[Session, Depends(get_db)]
Manager = Annotated[str, Depends(require_manager)]
Mutation = Annotated[None, Depends(require_control_request)]


def get_opencode_client() -> OpenCodeClient:
    settings = get_settings()
    return OpenCodeClient(
        settings.opencode_internal_url,
        settings.opencode_username,
        settings.opencode_password,
    )


OpenCode = Annotated[OpenCodeClient, Depends(get_opencode_client)]


@app.get("/health")
def health(db: DbSession) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: Manager) -> HTMLResponse:
    settings = get_settings()
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"opencode_url": settings.opencode_url},
    )


@app.get("/api/summary")
def summary(db: DbSession, _: Manager) -> dict:
    counts = dict(
        db.execute(select(Task.status, func.count(Task.id)).group_by(Task.status)).all()
    )
    pending_approvals = db.scalar(
        select(func.count(Approval.id)).where(Approval.status == "pending")
    )
    total_budget = db.scalar(
        select(func.coalesce(func.sum(Budget.monthly_limit), 0)).where(Budget.enabled.is_(True))
    )
    return {
        "tasks": counts,
        "pending_approvals": pending_approvals or 0,
        "month_cost": str(current_month_cost(db)),
        "configured_budget": str(total_budget or 0),
    }


@app.get("/api/tasks", response_model=list[TaskRead])
def list_tasks(
    db: DbSession,
    _: Manager,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[Task]:
    return list(db.scalars(select(Task).order_by(Task.created_at.desc()).limit(limit)))


@app.post("/api/tasks", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskCreate,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> Task:
    task = Task(**payload.model_dump())
    db.add(task)
    db.flush()
    write_audit(
        db,
        actor=manager,
        action="task.created",
        entity_type="task",
        entity_id=task.id,
        details={"domain": task.domain, "risk_level": task.risk_level},
    )
    db.commit()
    db.refresh(task)
    return task


@app.patch("/api/tasks/{task_id}/status", response_model=TaskRead)
def update_task_status(
    task_id: str,
    payload: TaskStatusUpdate,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> Task:
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if payload.status not in TASK_TRANSITIONS[task.status]:
        raise HTTPException(
            status_code=409,
            detail=f"Недопустимый переход: {task.status} → {payload.status}",
        )
    previous = task.status
    task.status = payload.status
    task.updated_at = datetime.now(timezone.utc)
    write_audit(
        db,
        actor=manager,
        action="task.status_changed",
        entity_type="task",
        entity_id=task.id,
        details={"from": previous, "to": payload.status},
    )
    db.commit()
    db.refresh(task)
    return task


@app.get("/api/approvals", response_model=list[ApprovalRead])
def list_approvals(
    db: DbSession,
    _: Manager,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[Approval]:
    return list(db.scalars(select(Approval).order_by(Approval.created_at.desc()).limit(limit)))


@app.post("/api/approvals", response_model=ApprovalRead, status_code=status.HTTP_201_CREATED)
def create_approval(
    payload: ApprovalCreate,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> Approval:
    if payload.task_id and db.get(Task, payload.task_id) is None:
        raise HTTPException(status_code=404, detail="Связанная задача не найдена")
    approval = Approval(**payload.model_dump())
    db.add(approval)
    db.flush()
    write_audit(
        db,
        actor=manager,
        action="approval.requested",
        entity_type="approval",
        entity_id=approval.id,
        details={"kind": approval.kind, "task_id": approval.task_id},
    )
    db.commit()
    db.refresh(approval)
    return approval


@app.post("/api/approvals/{approval_id}/decision", response_model=ApprovalRead)
def decide_approval(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> Approval:
    approval = db.scalar(select(Approval).where(Approval.id == approval_id).with_for_update())
    if approval is None:
        raise HTTPException(status_code=404, detail="Согласование не найдено")
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail="Решение уже принято")
    approval.status = payload.decision
    approval.decided_by = manager
    approval.decision_comment = payload.comment
    approval.decided_at = datetime.now(timezone.utc)
    write_audit(
        db,
        actor=manager,
        action=f"approval.{payload.decision}",
        entity_type="approval",
        entity_id=approval.id,
        details={"kind": approval.kind, "comment": payload.comment},
    )
    db.commit()
    db.refresh(approval)
    return approval


@app.get("/api/budgets", response_model=list[BudgetRead])
def list_budgets(db: DbSession, _: Manager) -> list[Budget]:
    return list(db.scalars(select(Budget).order_by(Budget.scope)))


@app.put("/api/budgets/{scope}", response_model=BudgetRead)
def upsert_budget(
    scope: str,
    payload: BudgetUpdate,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> Budget:
    if not scope or len(scope) > 80:
        raise HTTPException(status_code=422, detail="Некорректная область бюджета")
    budget = db.get(Budget, scope)
    if budget is None:
        budget = Budget(scope=scope, **payload.model_dump())
        db.add(budget)
        action = "budget.created"
    else:
        for key, value in payload.model_dump().items():
            setattr(budget, key, value)
        action = "budget.updated"
    db.flush()
    write_audit(
        db,
        actor=manager,
        action=action,
        entity_type="budget",
        entity_id=scope,
        details={"monthly_limit": str(payload.monthly_limit), "hard_stop": payload.hard_stop},
    )
    db.commit()
    db.refresh(budget)
    return budget


@app.post("/api/usage", response_model=UsageRead, status_code=status.HTTP_201_CREATED)
def record_usage(
    payload: UsageCreate,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> UsageEvent:
    if payload.task_id and db.get(Task, payload.task_id) is None:
        raise HTTPException(status_code=404, detail="Связанная задача не найдена")
    event = UsageEvent(**payload.model_dump())
    db.add(event)
    db.flush()
    write_audit(
        db,
        actor=manager,
        action="usage.recorded",
        entity_type="usage",
        entity_id=event.id,
        details={"model": event.model, "cost": str(event.cost)},
    )
    db.commit()
    db.refresh(event)
    return event


@app.get("/api/audit", response_model=list[AuditRead])
def list_audit(
    db: DbSession,
    _: Manager,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditEvent]:
    return list(
        db.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit))
    )


@app.get("/api/capabilities/guard", response_model=CapabilityGuardRead)
def get_capability_guard(db: DbSession, _: Manager) -> CapabilityGuard:
    guard = db.get(CapabilityGuard, 1)
    if guard is None:
        raise HTTPException(status_code=503, detail="Предохранитель возможностей не инициализирован")
    return guard


@app.get("/api/executions", response_model=list[ExecutionRead])
def list_executions(
    db: DbSession,
    _: Manager,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[ExecutionRun]:
    return list(
        db.scalars(select(ExecutionRun).order_by(ExecutionRun.created_at.desc()).limit(limit))
    )


@app.post("/api/tasks/{task_id}/execute", response_model=ExecutionRead, status_code=status.HTTP_201_CREATED)
def start_execution(
    task_id: str,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> ExecutionRun:
    """Persist execution intent transactionally; the worker owns external dispatch."""
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.domain != "development":
        raise HTTPException(status_code=409, detail="Execution V1 пока поддерживает только development-задачи")
    if task.status == "done":
        raise HTTPException(status_code=409, detail="Завершенную задачу нельзя запустить повторно")
    active = db.scalar(
        select(ExecutionRun).where(
            ExecutionRun.task_id == task.id,
            ExecutionRun.status.in_(("queued", "running")),
        )
    )
    if active is not None:
        raise HTTPException(status_code=409, detail="Для задачи уже есть активный запуск")

    run = ExecutionRun(
        task_id=task.id,
        status="queued",
        stage="dispatch_pending",
        opencode_session_id=None,
        assigned_roles=["department-lead"],
    )
    db.add(run)
    if task.status in {"backlog", "planned", "failed"}:
        task.status = "in_progress"
    task.updated_at = datetime.now(timezone.utc)
    db.flush()
    write_audit(
        db,
        actor=manager,
        action="execution.queued",
        entity_type="execution",
        entity_id=run.id,
        details={"task_id": task.id},
    )
    db.commit()
    db.refresh(run)
    return run


@app.post("/api/executions/{execution_id}/refresh", response_model=ExecutionRead)
def refresh_execution(
    execution_id: str,
    db: DbSession,
    manager: Manager,
    _: Mutation,
    opencode: OpenCode,
) -> ExecutionRun:
    run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == execution_id).with_for_update())
    if run is None:
        raise HTTPException(status_code=404, detail="Запуск не найден")
    if run.status != "running" or not run.opencode_session_id:
        return run
    try:
        statuses = opencode.session_statuses()
        messages = opencode.messages(run.opencode_session_id)
    except OpenCodeError as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось получить статус OpenCode: {exc}") from exc

    state = statuses.get(run.opencode_session_id) or {}
    state_type = state.get("type") if isinstance(state, dict) else str(state)
    result = extract_last_assistant_text(messages)
    if state_type == "idle" and result:
        run.status = "completed"
        run.stage = "manager_review"
        run.result = result
        run.finished_at = datetime.now(timezone.utc)
        task = db.get(Task, run.task_id)
        if task and task.status in {"in_progress", "waiting_approval"}:
            task.status = "qa"
            task.updated_at = datetime.now(timezone.utc)
        write_audit(
            db,
            actor=manager,
            action="execution.completed",
            entity_type="execution",
            entity_id=run.id,
            details={"task_id": run.task_id},
        )
    run.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)
    return run


@app.post("/api/executions/{execution_id}/abort", response_model=ExecutionRead)
def abort_execution(
    execution_id: str,
    db: DbSession,
    manager: Manager,
    _: Mutation,
    opencode: OpenCode,
) -> ExecutionRun:
    run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == execution_id).with_for_update())
    if run is None:
        raise HTTPException(status_code=404, detail="Запуск не найден")
    if run.status == "queued":
        run.status = "cancelled"
        run.stage = "stopped"
        run.lease_owner = None
        run.lease_expires_at = None
        run.finished_at = datetime.now(timezone.utc)
        run.updated_at = datetime.now(timezone.utc)
        write_audit(
            db,
            actor=manager,
            action="execution.cancelled",
            entity_type="execution",
            entity_id=run.id,
            details={"task_id": run.task_id, "phase": "dispatch"},
        )
        db.commit()
        db.refresh(run)
        return run
    if run.status != "running":
        return run
    if not run.opencode_session_id:
        raise HTTPException(status_code=409, detail="Активный запуск не имеет OpenCode session id")
    try:
        opencode.abort(run.opencode_session_id)
    except OpenCodeError as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось остановить OpenCode: {exc}") from exc
    run.status = "cancelled"
    run.stage = "stopped"
    run.lease_owner = None
    run.lease_expires_at = None
    run.finished_at = datetime.now(timezone.utc)
    run.updated_at = datetime.now(timezone.utc)
    write_audit(
        db,
        actor=manager,
        action="execution.cancelled",
        entity_type="execution",
        entity_id=run.id,
        details={"task_id": run.task_id, "phase": "running"},
    )
    db.commit()
    db.refresh(run)
    return run


def _message_time(info: dict):
    raw = info.get("created_at") or info.get("createdAt") or info.get("time")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _progress_items(messages: list[dict]) -> list[dict]:
    items = []
    for item in messages[-30:]:
        info = item.get("info") or {}
        role = str(info.get("agent") or info.get("role") or "assistant")
        model = info.get("model") or info.get("modelID")
        chunks = [
            str(part["text"]).strip()
            for part in (item.get("parts") or [])
            if part.get("type") == "text" and part.get("text")
        ]
        text_value = "\n".join(chunk for chunk in chunks if chunk).strip()
        if not text_value:
            continue
        items.append({
            "role": role,
            "model": str(model) if model else None,
            "text": text_value[:4000],
            "created_at": _message_time(info),
        })
    return items[-12:]


def _execution_elapsed_seconds(run: ExecutionRun) -> int:
    now = run.finished_at or datetime.now(timezone.utc)
    created = run.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0, int((now - created).total_seconds()))


@app.get("/api/executions/{execution_id}/progress", response_model=ExecutionProgressRead)
def execution_progress(
    execution_id: str,
    db: DbSession,
    _: Manager,
    opencode: OpenCode,
) -> dict:
    run = db.get(ExecutionRun, execution_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Запуск не найден")
    if not run.opencode_session_id:
        return {
            "execution_id": run.id,
            "status": run.status,
            "stage": run.stage,
            "session_state": "queued" if run.status == "queued" else "unknown",
            "elapsed_seconds": _execution_elapsed_seconds(run),
            "items": [],
        }
    try:
        statuses = opencode.session_statuses()
        messages = opencode.messages(run.opencode_session_id)
    except OpenCodeError as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось получить прогресс OpenCode: {exc}") from exc
    state = statuses.get(run.opencode_session_id) or {}
    state_type = state.get("type") if isinstance(state, dict) else str(state)
    return {
        "execution_id": run.id,
        "status": run.status,
        "stage": run.stage,
        "session_state": state_type or ("dispatching" if run.status == "queued" else "unknown"),
        "elapsed_seconds": _execution_elapsed_seconds(run),
        "items": _progress_items(messages),
    }
