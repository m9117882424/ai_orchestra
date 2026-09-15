from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import require_control_request, require_manager
from .db import get_db
from .models import (
    Approval,
    AuditEvent,
    Budget,
    CapabilityGuard,
    ExecutionRun,
    Repository,
    Task,
    TaskWorkspace,
    UsageEvent,
    new_id,
)
from .repository_policy import (
    RepositoryPolicyError,
    normalize_repository_remote,
    validate_assurance_configuration,
)
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
    RepositoryCreate,
    RepositoryProvider,
    RepositoryRead,
    RepositoryStatus,
    RepositoryUpdate,
    RepositoryValidationRequest,
    TaskCreate,
    TaskRead,
    TaskRepositoryUpdate,
    TaskStatusUpdate,
    TaskWorkspaceRead,
    UsageCreate,
    UsageRead,
    WorkspaceCleanupRequest,
)
from .services import current_month_cost, seed_defaults, write_audit
from .opencode_client import OpenCodeClient, OpenCodeError
from .settings import get_settings
from .workspace_protocol import (
    WorkspacePreflightError,
    canonical_uuid,
    normalize_commit,
    validate_branch,
    workspace_branch_name,
    workspace_path_for,
)


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
    version="0.9.0",
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


def _repository_conflict_detail(exc: IntegrityError) -> str | None:
    constraint_name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    if constraint_name == "ux_repositories_name":
        return "Имя репозитория уже зарегистрировано"
    if constraint_name == "ux_repositories_remote_identity":
        return "Git remote уже зарегистрирован"

    # SQLite does not expose a structured constraint name. This branch is also
    # exercised by the API test suite, while production PostgreSQL uses diag.
    message = str(exc.orig).lower()
    if "unique constraint failed: repositories.name" in message:
        return "Имя репозитория уже зарегистрировано"
    if "unique constraint failed: repositories.remote_identity" in message:
        return "Git remote уже зарегистрирован"
    return None


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


@app.get("/api/repositories", response_model=list[RepositoryRead])
def list_repositories(
    db: DbSession,
    _: Manager,
    enabled: Annotated[bool | None, Query()] = None,
    repository_status: Annotated[RepositoryStatus | None, Query(alias="status")] = None,
    provider: Annotated[RepositoryProvider | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[Repository]:
    statement = select(Repository)
    if enabled is not None:
        statement = statement.where(Repository.enabled.is_(enabled))
    if repository_status is not None:
        statement = statement.where(Repository.status == repository_status)
    if provider is not None:
        statement = statement.where(Repository.provider == provider)
    return list(db.scalars(statement.order_by(Repository.name).limit(limit)))


@app.get("/api/repositories/{repository_id}", response_model=RepositoryRead)
def get_repository(repository_id: str, db: DbSession, _: Manager) -> Repository:
    repository = db.get(Repository, repository_id)
    if repository is None:
        raise HTTPException(status_code=404, detail="Репозиторий не найден")
    return repository


@app.post(
    "/api/repositories",
    response_model=RepositoryRead,
    status_code=status.HTTP_201_CREATED,
)
def create_repository(
    payload: RepositoryCreate,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> Repository:
    now = datetime.now(timezone.utc)
    remote = normalize_repository_remote(payload.remote_url)
    if db.scalar(select(Repository.id).where(Repository.name == payload.name)) is not None:
        raise HTTPException(status_code=409, detail="Имя репозитория уже зарегистрировано")
    if (
        db.scalar(
            select(Repository.id).where(Repository.remote_identity == remote.identity)
        )
        is not None
    ):
        raise HTTPException(status_code=409, detail="Git remote уже зарегистрирован")

    repository = Repository(
        name=payload.name,
        remote_url=remote.url,
        remote_identity=remote.identity,
        remote_host=remote.host,
        provider=remote.provider,
        auth_profile_ref=payload.auth_profile_ref,
        enabled=payload.enabled,
        status="pending_validation",
        execution_profile=payload.execution_profile,
        assurance_tier=payload.assurance_tier,
        assurance_profile=payload.assurance_profile,
        sync_requested_at=now,
        sync_next_at=now if payload.enabled else None,
        version=1,
    )
    db.add(repository)
    try:
        db.flush()
        write_audit(
            db,
            actor=manager,
            action="repository.registered",
            entity_type="repository",
            entity_id=repository.id,
            details={
                "name": repository.name,
                "provider": repository.provider,
                "remote_host": repository.remote_host,
                "enabled": repository.enabled,
                "assurance_tier": repository.assurance_tier,
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        detail = _repository_conflict_detail(exc)
        if detail is None:
            raise
        raise HTTPException(status_code=409, detail=detail) from exc
    db.refresh(repository)
    return repository


@app.patch("/api/repositories/{repository_id}", response_model=RepositoryRead)
def update_repository(
    repository_id: str,
    payload: RepositoryUpdate,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> Repository:
    repository = db.scalar(
        select(Repository).where(Repository.id == repository_id).with_for_update()
    )
    if repository is None:
        raise HTTPException(status_code=404, detail="Репозиторий не найден")
    if repository.version != payload.expected_version:
        raise HTTPException(
            status_code=409,
            detail=(
                "Запись репозитория уже изменена: "
                f"expected_version={payload.expected_version}, current_version={repository.version}"
            ),
        )

    updates = payload.model_dump(exclude_unset=True)
    updates.pop("expected_version")
    next_tier = updates.get("assurance_tier", repository.assurance_tier)
    next_profile = updates.get("assurance_profile", repository.assurance_profile)
    try:
        validate_assurance_configuration(next_tier, next_profile)
    except RepositoryPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    changes: dict[str, dict[str, object]] = {}
    for field_name, value in updates.items():
        previous = getattr(repository, field_name)
        if previous == value:
            continue
        setattr(repository, field_name, value)
        if field_name == "auth_profile_ref":
            changes[field_name] = {"changed": True}
        else:
            changes[field_name] = {"from": previous, "to": value}

    if not changes:
        return repository

    repository.version += 1
    now = datetime.now(timezone.utc)
    if "auth_profile_ref" in changes or "enabled" in changes:
        repository.status = "pending_validation"
        repository.sync_requested_at = now
        repository.sync_next_at = now if repository.enabled else None
        repository.last_sync_error_code = None
    repository.updated_at = now
    write_audit(
        db,
        actor=manager,
        action="repository.updated",
        entity_type="repository",
        entity_id=repository.id,
        details={"version": repository.version, "changes": changes},
    )
    db.commit()
    db.refresh(repository)
    return repository


@app.post(
    "/api/repositories/{repository_id}/validate",
    response_model=RepositoryRead,
)
def request_repository_validation(
    repository_id: str,
    payload: RepositoryValidationRequest,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> Repository:
    repository = db.scalar(
        select(Repository).where(Repository.id == repository_id).with_for_update()
    )
    if repository is None:
        raise HTTPException(status_code=404, detail="Репозиторий не найден")
    if repository.version != payload.expected_version:
        raise HTTPException(
            status_code=409,
            detail=(
                "Запись репозитория уже изменена: "
                f"expected_version={payload.expected_version}, current_version={repository.version}"
            ),
        )
    if not repository.enabled:
        raise HTTPException(
            status_code=409,
            detail="Отключенный репозиторий нельзя проверить; сначала включите его",
        )

    now = datetime.now(timezone.utc)
    repository.status = "pending_validation"
    repository.sync_requested_at = now
    repository.sync_next_at = now
    repository.last_sync_error_code = None
    repository.version += 1
    repository.updated_at = now
    write_audit(
        db,
        actor=manager,
        action="repository.validation_requested",
        entity_type="repository",
        entity_id=repository.id,
        details={"version": repository.version},
    )
    db.commit()
    db.refresh(repository)
    return repository


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
    if payload.repository_id and db.get(Repository, payload.repository_id) is None:
        raise HTTPException(status_code=404, detail="Репозиторий задачи не найден")
    task = Task(**payload.model_dump())
    db.add(task)
    db.flush()
    write_audit(
        db,
        actor=manager,
        action="task.created",
        entity_type="task",
        entity_id=task.id,
        details={
            "domain": task.domain,
            "risk_level": task.risk_level,
            "repository_id": task.repository_id,
        },
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


@app.patch("/api/tasks/{task_id}/repository", response_model=TaskRead)
def update_task_repository(
    task_id: str,
    payload: TaskRepositoryUpdate,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> Task:
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    active = db.scalar(
        select(ExecutionRun.id).where(
            ExecutionRun.task_id == task.id,
            ExecutionRun.status.in_(("preparing", "queued", "running")),
        )
    )
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail="Нельзя менять репозиторий во время активного запуска",
        )
    if payload.repository_id and db.get(Repository, payload.repository_id) is None:
        raise HTTPException(status_code=404, detail="Репозиторий задачи не найден")
    previous = task.repository_id
    if previous == payload.repository_id:
        return task
    task.repository_id = payload.repository_id
    task.updated_at = datetime.now(timezone.utc)
    write_audit(
        db,
        actor=manager,
        action="task.repository_changed",
        entity_type="task",
        entity_id=task.id,
        details={"from": previous, "to": payload.repository_id},
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


@app.get("/api/workspaces", response_model=list[TaskWorkspaceRead])
def list_workspaces(
    db: DbSession,
    _: Manager,
    task_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[TaskWorkspace]:
    statement = select(TaskWorkspace)
    if task_id is not None:
        statement = statement.where(TaskWorkspace.task_id == task_id)
    return list(
        db.scalars(statement.order_by(TaskWorkspace.created_at.desc()).limit(limit))
    )


@app.get("/api/workspaces/{workspace_id}", response_model=TaskWorkspaceRead)
def get_workspace(workspace_id: str, db: DbSession, _: Manager) -> TaskWorkspace:
    workspace = db.get(TaskWorkspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Рабочий каталог не найден")
    return workspace


@app.post("/api/workspaces/{workspace_id}/cleanup", response_model=TaskWorkspaceRead)
def request_workspace_cleanup(
    workspace_id: str,
    payload: WorkspaceCleanupRequest,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> TaskWorkspace:
    workspace = db.scalar(
        select(TaskWorkspace)
        .where(TaskWorkspace.id == workspace_id)
        .with_for_update()
    )
    if workspace is None:
        raise HTTPException(status_code=404, detail="Рабочий каталог не найден")
    if workspace.version != payload.expected_version:
        raise HTTPException(
            status_code=409,
            detail=(
                "Запись рабочего каталога уже изменена: "
                f"expected_version={payload.expected_version}, current_version={workspace.version}"
            ),
        )
    if workspace.status != "retained":
        raise HTTPException(
            status_code=409,
            detail="Удаление разрешено только после завершенной проверки рабочего каталога",
        )
    if (
        workspace.has_changes is not False
        or workspace.current_head_commit != workspace.base_commit
        or workspace.current_tree != workspace.initial_tree
    ):
        raise HTTPException(
            status_code=409,
            detail="Рабочий каталог изменён или не подтверждён; автоматическое удаление запрещено",
        )
    run = db.scalar(
        select(ExecutionRun).where(ExecutionRun.workspace_id == workspace.id)
    )
    if run is None or run.status not in {"completed", "failed", "cancelled"}:
        raise HTTPException(
            status_code=409,
            detail="Связанный запуск не находится в терминальном состоянии",
        )
    now = datetime.now(timezone.utc)
    workspace.status = "cleanup_pending"
    workspace.cleanup_requested_at = now
    workspace.next_attempt_at = now
    workspace.last_error_code = None
    workspace.version += 1
    workspace.updated_at = now
    write_audit(
        db,
        actor=manager,
        action="workspace.cleanup_requested",
        entity_type="workspace",
        entity_id=workspace.id,
        details={"execution_id": run.id, "expected_version": payload.expected_version},
    )
    db.commit()
    db.refresh(workspace)
    return workspace


@app.post("/api/tasks/{task_id}/execute", response_model=ExecutionRead, status_code=status.HTTP_201_CREATED)
def start_execution(
    task_id: str,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> ExecutionRun:
    """Persist a repository-bound intent; no inference occurs before preflight."""
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if task.domain != "development":
        raise HTTPException(status_code=409, detail="Execution V1 пока поддерживает только development-задачи")
    if task.status == "done":
        raise HTTPException(status_code=409, detail="Завершенную задачу нельзя запустить повторно")
    if task.repository_id is None:
        raise HTTPException(
            status_code=409,
            detail="Для запуска задачи сначала назначьте репозиторий",
        )
    active = db.scalar(
        select(ExecutionRun).where(
            ExecutionRun.task_id == task.id,
            ExecutionRun.status.in_(("preparing", "queued", "running")),
        )
    )
    if active is not None:
        raise HTTPException(status_code=409, detail="Для задачи уже есть активный запуск")

    repository = db.scalar(
        select(Repository)
        .where(Repository.id == task.repository_id)
        .with_for_update()
    )
    if repository is None:
        raise HTTPException(status_code=409, detail="Репозиторий задачи больше не существует")
    if (
        not repository.enabled
        or repository.status != "ready"
        or repository.default_branch is None
        or repository.last_known_commit is None
    ):
        raise HTTPException(
            status_code=409,
            detail="Репозиторий не готов к безопасному запуску; дождитесь успешной синхронизации",
        )

    now = datetime.now(timezone.utc)
    run_id = new_id()
    workspace_id = new_id()
    try:
        canonical_uuid(task.id, field="task_id")
        canonical_uuid(repository.id, field="repository_id")
        base_commit = normalize_commit(repository.last_known_commit)
        base_branch = validate_branch(repository.default_branch)
        branch_name = workspace_branch_name(task.id, run_id)
    except WorkspacePreflightError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "Репозиторий или задача содержат некорректную immutable identity; "
                "повторите безопасную синхронизацию"
            ),
        ) from exc
    workspace_path = workspace_path_for(workspace_id)
    workspace = TaskWorkspace(
        id=workspace_id,
        task_id=task.id,
        repository_id=repository.id,
        status="pending",
        base_commit=base_commit,
        base_branch=base_branch,
        branch_name=branch_name,
        opencode_path=workspace_path,
        requested_at=now,
        next_attempt_at=now,
        created_at=now,
        updated_at=now,
    )
    run = ExecutionRun(
        id=run_id,
        task_id=task.id,
        contract_version=2,
        repository_id=repository.id,
        workspace_id=workspace_id,
        base_commit=base_commit,
        workspace_path=workspace_path,
        status="preparing",
        stage="workspace_pending",
        opencode_session_id=None,
        assigned_roles=["department-lead"],
        deadline_at=now + timedelta(seconds=get_settings().execution_timeout_seconds),
        created_at=now,
        updated_at=now,
    )
    db.add_all([workspace, run])
    if task.status in {"backlog", "planned", "failed"}:
        task.status = "in_progress"
    task.updated_at = now
    db.flush()
    write_audit(
        db,
        actor=manager,
        action="workspace.requested",
        entity_type="workspace",
        entity_id=workspace.id,
        details={
            "task_id": task.id,
            "execution_id": run.id,
            "repository_id": repository.id,
            "base_commit": base_commit,
            "base_branch": base_branch,
            "branch_name": branch_name,
        },
    )
    write_audit(
        db,
        actor=manager,
        action="execution.preparing",
        entity_type="execution",
        entity_id=run.id,
        details={
            "task_id": task.id,
            "repository_id": repository.id,
            "workspace_id": workspace.id,
            "base_commit": base_commit,
            "contract_version": 2,
        },
    )
    db.commit()
    db.refresh(run)
    return run


@app.post("/api/executions/{execution_id}/abort", response_model=ExecutionRead)
def abort_execution(
    execution_id: str,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> ExecutionRun:
    run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == execution_id).with_for_update())
    if run is None:
        raise HTTPException(status_code=404, detail="Запуск не найден")
    if run.status not in {"preparing", "queued", "running"} or run.cancel_requested_at is not None:
        return run

    now = datetime.now(timezone.utc)
    if run.status == "preparing":
        run.cancel_requested_at = now
        run.status = "cancelled"
        run.stage = "stopped"
        run.error = ""
        run.finished_at = now
        run.heartbeat_at = now
        run.lease_generation = int(run.lease_generation or 0) + 1
        run.lease_owner = None
        run.lease_expires_at = None
        run.updated_at = now
        workspace = db.scalar(
            select(TaskWorkspace)
            .where(TaskWorkspace.id == run.workspace_id)
            .with_for_update()
        )
        if workspace is None:
            raise HTTPException(
                status_code=500,
                detail="Нарушена связка запуска с рабочим каталогом",
            )
        workspace.status = "cleanup_pending"
        workspace.cleanup_requested_at = now
        workspace.next_attempt_at = now
        workspace.generation += 1
        workspace.lease_owner = None
        workspace.lease_expires_at = None
        workspace.last_error_code = None
        workspace.version += 1
        workspace.updated_at = now
        task = db.get(Task, run.task_id)
        if task and task.status in {"in_progress", "waiting_approval"}:
            task.status = "failed"
            task.updated_at = now
        write_audit(
            db,
            actor=manager,
            action="workspace.cleanup_requested",
            entity_type="workspace",
            entity_id=workspace.id,
            details={"execution_id": run.id, "reason": "preflight_cancelled"},
        )
        write_audit(
            db,
            actor=manager,
            action="execution.cancelled",
            entity_type="execution",
            entity_id=run.id,
            details={"task_id": run.task_id, "previous_status": "preparing"},
        )
        db.commit()
        db.refresh(run)
        return run

    run.cancel_requested_at = now
    run.stage = "cancel_requested"
    run.error = ""
    # Immediately fence a worker that may still hold the previous generation.
    run.lease_generation = int(run.lease_generation or 0) + 1
    run.lease_owner = None
    run.lease_expires_at = None
    run.updated_at = now
    write_audit(
        db,
        actor=manager,
        action="execution.cancel_requested",
        entity_type="execution",
        entity_id=run.id,
        details={"task_id": run.task_id, "status": run.status},
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
    if run.status not in {"queued", "running"}:
        return {
            "execution_id": run.id,
            "status": run.status,
            "stage": run.stage,
            "session_state": run.status,
            "elapsed_seconds": _execution_elapsed_seconds(run),
            "heartbeat_at": run.heartbeat_at,
            "deadline_at": run.deadline_at,
            "cancel_requested_at": run.cancel_requested_at,
            "lease_generation": run.lease_generation,
            "error": run.error,
            "items": [],
        }
    if not run.opencode_session_id:
        return {
            "execution_id": run.id,
            "status": run.status,
            "stage": run.stage,
            "session_state": "queued" if run.status == "queued" else "unknown",
            "elapsed_seconds": _execution_elapsed_seconds(run),
            "heartbeat_at": run.heartbeat_at,
            "deadline_at": run.deadline_at,
            "cancel_requested_at": run.cancel_requested_at,
            "lease_generation": run.lease_generation,
            "error": run.error,
            "items": [],
        }
    scoped_opencode = opencode
    if run.contract_version == 2:
        if not run.workspace_path:
            raise HTTPException(
                status_code=503,
                detail="У запуска отсутствует неизменяемая привязка рабочего каталога",
            )
        scoped_opencode = opencode.for_directory(run.workspace_path)
    try:
        statuses = scoped_opencode.session_statuses()
        messages = scoped_opencode.messages(run.opencode_session_id)
    except OpenCodeError as exc:
        return {
            "execution_id": run.id,
            "status": run.status,
            "stage": run.stage,
            "session_state": "unavailable",
            "elapsed_seconds": _execution_elapsed_seconds(run),
            "heartbeat_at": run.heartbeat_at,
            "deadline_at": run.deadline_at,
            "cancel_requested_at": run.cancel_requested_at,
            "lease_generation": run.lease_generation,
            "error": str(exc)[:500],
            "items": [],
        }
    state = statuses.get(run.opencode_session_id) or {}
    state_type = state.get("type") if isinstance(state, dict) else str(state)
    return {
        "execution_id": run.id,
        "status": run.status,
        "stage": run.stage,
        "session_state": state_type or ("dispatching" if run.status == "queued" else "unknown"),
        "elapsed_seconds": _execution_elapsed_seconds(run),
        "heartbeat_at": run.heartbeat_at,
        "deadline_at": run.deadline_at,
        "cancel_requested_at": run.cancel_requested_at,
        "lease_generation": run.lease_generation,
        "error": run.error,
        "items": _progress_items(messages),
    }
