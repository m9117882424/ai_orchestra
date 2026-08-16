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
from .db import Base, engine, get_db
from .models import Approval, AuditEvent, Budget, Task, TradingGuard, UsageEvent
from .schemas import (
    ApprovalCreate,
    ApprovalDecisionRequest,
    ApprovalRead,
    AuditRead,
    BudgetRead,
    BudgetUpdate,
    TaskCreate,
    TaskRead,
    TaskStatusUpdate,
    TradingGuardRead,
    UsageCreate,
    UsageRead,
)
from .services import current_month_cost, seed_defaults, write_audit
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
    Base.metadata.create_all(bind=engine)
    from .db import SessionLocal

    with SessionLocal() as db:
        seed_defaults(db, get_settings().default_monthly_budget)
    yield


app = FastAPI(
    title="AI Orchestra Control Plane",
    version="0.2.0",
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


@app.get("/api/trading/guard", response_model=TradingGuardRead)
def get_trading_guard(db: DbSession, _: Manager) -> TradingGuard:
    guard = db.get(TradingGuard, 1)
    if guard is None:
        raise HTTPException(status_code=503, detail="Торговый предохранитель не инициализирован")
    return guard
