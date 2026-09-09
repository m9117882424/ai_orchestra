from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database_base import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('github', 'gitlab', 'bitbucket', 'generic')",
            name="ck_repositories_provider",
        ),
        CheckConstraint(
            "status IN ('pending_validation', 'validating', 'ready', 'unavailable', 'invalid')",
            name="ck_repositories_status",
        ),
        CheckConstraint(
            "execution_profile = 'development'",
            name="ck_repositories_execution_profile",
        ),
        CheckConstraint(
            "assurance_tier IN "
            "('general-standard', 'general-high-assurance', 'regulated-critical')",
            name="ck_repositories_assurance_tier",
        ),
        CheckConstraint(
            "((assurance_tier = 'regulated-critical' AND assurance_profile IS NOT NULL "
            "AND length(assurance_profile) BETWEEN 1 AND 80) "
            "OR (assurance_tier <> 'regulated-critical' AND assurance_profile IS NULL))",
            name="ck_repositories_assurance_profile",
        ),
        CheckConstraint("version >= 1", name="ck_repositories_version"),
        CheckConstraint(
            "sync_generation >= 0",
            name="ck_repositories_sync_generation",
        ),
        CheckConstraint(
            "sync_failure_count >= 0",
            name="ck_repositories_sync_failure_count",
        ),
        CheckConstraint(
            "((sync_lease_owner IS NULL AND sync_lease_expires_at IS NULL) "
            "OR (sync_lease_owner IS NOT NULL AND sync_lease_expires_at IS NOT NULL))",
            name="ck_repositories_sync_lease_pair",
        ),
        CheckConstraint(
            "(status <> 'ready' OR (enabled IS TRUE "
            "AND length(default_branch) BETWEEN 1 AND 255 "
            "AND length(last_known_commit) IN (40, 64) "
            "AND last_fetched_at IS NOT NULL AND sync_finished_at IS NOT NULL "
            "AND sync_next_at IS NOT NULL AND sync_lease_owner IS NULL "
            "AND sync_lease_expires_at IS NULL AND last_sync_error_code IS NULL "
            "AND sync_failure_count = 0))",
            name="ck_repositories_ready_state",
        ),
        CheckConstraint(
            "(status <> 'validating' OR (enabled IS TRUE "
            "AND sync_started_at IS NOT NULL AND sync_lease_owner IS NOT NULL "
            "AND sync_lease_expires_at IS NOT NULL))",
            name="ck_repositories_validating_state",
        ),
        Index("ux_repositories_name", "name", unique=True),
        Index("ux_repositories_remote_identity", "remote_identity", unique=True),
        Index("ix_repositories_enabled_status", "enabled", "status"),
        Index("ix_repositories_sync_queue", "enabled", "sync_next_at"),
        Index("ix_repositories_sync_lease", "sync_lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    remote_url: Mapped[str] = mapped_column(String(1024))
    remote_identity: Mapped[str] = mapped_column(String(1024))
    remote_host: Mapped[str] = mapped_column(String(253))
    provider: Mapped[str] = mapped_column(String(24))
    auth_profile_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    default_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default="pending_validation")
    last_known_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sync_generation: Mapped[int] = mapped_column(Integer, default=0)
    sync_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    sync_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sync_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sync_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sync_next_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sync_lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sync_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_sync_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    execution_profile: Mapped[str] = mapped_column(String(40), default="development")
    assurance_tier: Mapped[str] = mapped_column(String(40), default="general-standard")
    assurance_profile: Mapped[str | None] = mapped_column(String(80), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    project: Mapped[str] = mapped_column(String(120), default="general")
    domain: Mapped[str] = mapped_column(String(32), default="development")
    priority: Mapped[str] = mapped_column(String(16), default="normal")
    status: Mapped[str] = mapped_column(String(32), default="backlog")
    risk_level: Mapped[str] = mapped_column(String(16), default="low")
    owner_role: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    requested_by: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Budget(Base):
    __tablename__ = "budgets"

    scope: Mapped[str] = mapped_column(String(80), primary_key=True)
    monthly_limit: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    warning_pct: Mapped[int] = mapped_column(Integer, default=80)
    hard_stop: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(120))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(80))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CapabilityGuard(Base):
    """Fail-closed capabilities owned by Orchestra itself.

    Product-specific policy (for example trading risk thresholds) belongs to the
    product repository, never to the development department control plane.
    """

    __tablename__ = "capability_guard"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    production_deploy_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    external_write_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    financial_execution_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    secret_access_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ExecutionRun(Base):
    __tablename__ = "execution_runs"
    __table_args__ = (
        Index("ix_execution_runs_active_deadline", "status", "deadline_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="running")
    stage: Mapped[str] = mapped_column(String(40), default="department_lead")
    # A queued execution is committed before any OpenCode side effect. The worker
    # fills this after creating or reconciling the external session.
    opencode_session_id: Mapped[str | None] = mapped_column(
        String(120), unique=True, index=True, nullable=True
    )
    lead_role: Mapped[str] = mapped_column(String(80), default="department-lead")
    assigned_roles: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_generation: Mapped[int] = mapped_column(Integer, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
