from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


TaskDomain = Literal["development", "analytics", "trading"]
TaskPriority = Literal["low", "normal", "high", "critical"]
TaskStatus = Literal[
    "backlog", "planned", "in_progress", "waiting_approval", "qa", "done", "failed"
]
RiskLevel = Literal["low", "medium", "high", "critical"]
ApprovalKind = Literal["code_change", "git_push", "deploy", "trading_mode", "live_order"]
ApprovalDecision = Literal["approved", "rejected"]


class TaskCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(default="", max_length=10000)
    project: str = Field(default="general", min_length=1, max_length=120)
    domain: TaskDomain = "development"
    priority: TaskPriority = "normal"
    risk_level: RiskLevel = "low"
    owner_role: str | None = Field(default=None, max_length=80)


class TaskStatusUpdate(BaseModel):
    status: TaskStatus


class TaskRead(TaskCreate):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime


class ApprovalCreate(BaseModel):
    task_id: str | None = None
    kind: ApprovalKind
    requested_by: str = Field(min_length=2, max_length=100)
    reason: str = Field(min_length=3, max_length=10000)


class ApprovalDecisionRequest(BaseModel):
    decision: ApprovalDecision
    comment: str = Field(default="", max_length=5000)


class ApprovalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str | None
    kind: ApprovalKind
    status: Literal["pending", "approved", "rejected"]
    requested_by: str
    reason: str
    decided_by: str | None
    decision_comment: str | None
    created_at: datetime
    decided_at: datetime | None


class BudgetUpdate(BaseModel):
    monthly_limit: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    warning_pct: int = Field(default=80, ge=1, le=100)
    hard_stop: bool = True
    enabled: bool = True


class BudgetRead(BudgetUpdate):
    model_config = ConfigDict(from_attributes=True)

    scope: str
    updated_at: datetime


class UsageCreate(BaseModel):
    task_id: str | None = None
    role: str = Field(min_length=1, max_length=80)
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=6)


class UsageRead(UsageCreate):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime


class AuditRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    actor: str
    action: str
    entity_type: str
    entity_id: str
    details: dict
    created_at: datetime


class TradingGuardRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    mode: str
    emergency_stop: bool
    live_order_enabled: bool
    auto_sell_enabled: bool
    min_deviation_pct: Decimal
    cash_reserve_min_pct: Decimal
    cash_reserve_max_pct: Decimal
    daily_purchase_limit: Decimal
    updated_at: datetime
