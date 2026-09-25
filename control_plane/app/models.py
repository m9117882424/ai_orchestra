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
            "AND default_branch IS NOT NULL "
            "AND length(default_branch) BETWEEN 1 AND 255 "
            "AND last_known_commit IS NOT NULL "
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
    repository_id: Mapped[str | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    domain: Mapped[str] = mapped_column(String(32), default="development")
    priority: Mapped[str] = mapped_column(String(16), default="normal")
    status: Mapped[str] = mapped_column(String(32), default="backlog")
    risk_level: Mapped[str] = mapped_column(String(16), default="low")
    owner_role: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class TaskWorkspace(Base):
    __tablename__ = "task_workspaces"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'preparing', 'unavailable', 'ready', "
            "'inspection_pending', 'inspecting', 'retained', "
            "'cleanup_pending', 'cleaning', 'removed', 'invalid')",
            name="ck_task_workspaces_status",
        ),
        CheckConstraint(
            "length(base_commit) IN (40, 64)",
            name="ck_task_workspaces_base_commit",
        ),
        CheckConstraint(
            "length(base_branch) BETWEEN 1 AND 255",
            name="ck_task_workspaces_base_branch",
        ),
        CheckConstraint(
            "length(branch_name) BETWEEN 1 AND 255",
            name="ck_task_workspaces_branch_name",
        ),
        CheckConstraint(
            "length(opencode_path) BETWEEN 1 AND 512",
            name="ck_task_workspaces_opencode_path",
        ),
        CheckConstraint("generation >= 0", name="ck_task_workspaces_generation"),
        CheckConstraint("failure_count >= 0", name="ck_task_workspaces_failure_count"),
        CheckConstraint("version >= 1", name="ck_task_workspaces_version"),
        CheckConstraint(
            "changed_file_count IS NULL OR changed_file_count >= 0",
            name="ck_task_workspaces_changed_file_count",
        ),
        CheckConstraint(
            "((lease_owner IS NULL AND lease_expires_at IS NULL) "
            "OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))",
            name="ck_task_workspaces_lease_pair",
        ),
        CheckConstraint(
            "((status IN ('preparing', 'inspecting', 'cleaning') "
            "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status NOT IN ('preparing', 'inspecting', 'cleaning') "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL))",
            name="ck_task_workspaces_active_lease",
        ),
        CheckConstraint(
            "((status IN ('pending', 'unavailable', 'inspection_pending', "
            "'cleanup_pending') AND next_attempt_at IS NOT NULL) "
            "OR (status NOT IN ('pending', 'unavailable', 'inspection_pending', "
            "'cleanup_pending') AND next_attempt_at IS NULL))",
            name="ck_task_workspaces_queue_state",
        ),
        CheckConstraint(
            "(status <> 'ready' OR (initial_tree IS NOT NULL "
            "AND length(initial_tree) IN (40, 64) "
            "AND preflight_digest IS NOT NULL "
            "AND length(preflight_digest) = 64 AND tracked_entries IS NOT NULL "
            "AND tracked_entries >= 0 "
            "AND prepared_at IS NOT NULL AND last_error_code IS NULL))",
            name="ck_task_workspaces_ready_state",
        ),
        CheckConstraint(
            "(status <> 'retained' OR (inspected_at IS NOT NULL "
            "AND has_changes IS NOT NULL AND changed_file_count IS NOT NULL))",
            name="ck_task_workspaces_retained_state",
        ),
        CheckConstraint(
            "(status <> 'removed' OR cleaned_at IS NOT NULL)",
            name="ck_task_workspaces_removed_state",
        ),
        Index("ux_task_workspaces_opencode_path", "opencode_path", unique=True),
        Index("ix_task_workspaces_task_id", "task_id"),
        Index("ix_task_workspaces_repository_id", "repository_id"),
        Index("ix_task_workspaces_queue", "status", "next_attempt_at"),
        Index("ix_task_workspaces_lease", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    repository_id: Mapped[str] = mapped_column(
        ForeignKey("repositories.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="pending")
    base_commit: Mapped[str] = mapped_column(String(64))
    base_branch: Mapped[str] = mapped_column(String(255))
    branch_name: Mapped[str] = mapped_column(String(255))
    opencode_path: Mapped[str] = mapped_column(String(512))
    initial_tree: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preflight_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tracked_entries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_head_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_tree: Mapped[str | None] = mapped_column(String(64), nullable=True)
    change_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    has_changes: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    changed_file_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    inspection_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    inspected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleanup_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
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


class ControlledAction(Base):
    __tablename__ = "controlled_actions"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('git_push', 'pull_request', 'merge', 'deploy', 'external_write')",
            name="ck_controlled_actions_type",
        ),
        CheckConstraint(
            "status IN ('proposed', 'pending_approval', 'approved', 'approval_rejected', "
            "'claimed', 'succeeded', 'failed', 'uncertain', 'reconciled')",
            name="ck_controlled_actions_status",
        ),
        CheckConstraint(
            "length(source_sha) IN (40, 64) AND length(head_sha) IN (40, 64)",
            name="ck_controlled_actions_sha_length",
        ),
        CheckConstraint(
            "result_package_digest IS NULL OR length(result_package_digest) = 64",
            name="ck_controlled_actions_result_package_digest",
        ),
        CheckConstraint(
            "length(action_digest) = 64",
            name="ck_controlled_actions_digest",
        ),
        CheckConstraint("version >= 1", name="ck_controlled_actions_version"),
        Index("ux_controlled_actions_digest", "action_digest", unique=True),
        Index("ix_controlled_actions_repository_status", "repository_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    repository_id: Mapped[str] = mapped_column(
        ForeignKey("repositories.id", ondelete="RESTRICT"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(32))
    source_sha: Mapped[str] = mapped_column(String(64))
    head_sha: Mapped[str] = mapped_column(String(64))
    destination: Mapped[str] = mapped_column(String(512))
    result_package_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    action_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    created_by: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ControlledActionAuthorization(Base):
    __tablename__ = "controlled_action_authorizations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'consumed')",
            name="ck_controlled_action_authorizations_status",
        ),
        CheckConstraint(
            "length(action_digest) = 64",
            name="ck_controlled_action_authorizations_digest",
        ),
        CheckConstraint(
            "((status IN ('approved', 'rejected', 'consumed') AND decided_at IS NOT NULL "
            "AND decided_by IS NOT NULL) OR status IN ('pending', 'expired'))",
            name="ck_controlled_action_authorizations_decision",
        ),
        CheckConstraint(
            "((status = 'consumed' AND consumed_at IS NOT NULL AND consumed_by IS NOT NULL "
            "AND operation_key IS NOT NULL) OR (status <> 'consumed' AND consumed_at IS NULL "
            "AND consumed_by IS NULL AND operation_key IS NULL))",
            name="ck_controlled_action_authorizations_consumption",
        ),
        Index(
            "ix_controlled_action_authorizations_action_status",
            "action_id", "status", "created_at",
        ),
        Index(
            "ux_controlled_action_authorizations_operation_key",
            "operation_key",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("controlled_actions.id", ondelete="CASCADE"), nullable=False
    )
    action_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    requested_by: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    operation_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ControlledActionEffect(Base):
    __tablename__ = "controlled_action_effects"
    __table_args__ = (
        CheckConstraint(
            "status IN ('reserved', 'succeeded', 'failed', 'uncertain', 'reconciled')",
            name="ck_controlled_action_effects_status",
        ),
        CheckConstraint(
            "length(action_digest) = 64",
            name="ck_controlled_action_effects_digest",
        ),
        CheckConstraint(
            "observed_before_digest IS NULL OR length(observed_before_digest) IN (40, 64)",
            name="ck_controlled_action_effects_before_digest",
        ),
        CheckConstraint(
            "result_digest IS NULL OR length(result_digest) IN (40, 64)",
            name="ck_controlled_action_effects_result_digest",
        ),
        Index("ux_controlled_action_effects_action", "action_id", unique=True),
        Index("ux_controlled_action_effects_operation_key", "operation_key", unique=True),
        Index("ix_controlled_action_effects_status", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    action_id: Mapped[str] = mapped_column(
        ForeignKey("controlled_actions.id", ondelete="CASCADE"), nullable=False
    )
    authorization_id: Mapped[str] = mapped_column(
        ForeignKey("controlled_action_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    action_digest: Mapped[str] = mapped_column(String(64))
    operation_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="reserved")
    claimed_by: Mapped[str] = mapped_column(String(100))
    external_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    observed_before_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    preflight_reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


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
    __table_args__ = (
        CheckConstraint(
            "((source IS NULL AND source_key IS NULL) OR "
            "(source IS NOT NULL AND source_key IS NOT NULL))",
            name="ck_usage_events_source_pair",
        ),
        Index(
            "ux_usage_events_source_key",
            "execution_id", "source", "source_key",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    child_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_child_runs.id", ondelete="SET NULL"), nullable=True, index=True
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
        CheckConstraint(
            "contract_version IN (1, 2)",
            name="ck_execution_runs_contract_version",
        ),
        CheckConstraint(
            "(contract_version = 1 OR (repository_id IS NOT NULL "
            "AND workspace_id IS NOT NULL AND base_commit IS NOT NULL "
            "AND length(base_commit) IN (40, 64) "
            "AND workspace_path IS NOT NULL "
            "AND length(workspace_path) BETWEEN 1 AND 512))",
            name="ck_execution_runs_workspace_binding",
        ),
        CheckConstraint(
            "(contract_version = 1 OR status NOT IN ('queued', 'running', 'completed') "
            "OR (workspace_tree IS NOT NULL "
            "AND length(workspace_tree) IN (40, 64) "
            "AND workspace_preflight_digest IS NOT NULL "
            "AND length(workspace_preflight_digest) = 64 "
            "AND workspace_preflight_completed_at IS NOT NULL))",
            name="ck_execution_runs_workspace_preflight",
        ),
        CheckConstraint(
            "(contract_version = 1 OR status NOT IN ('running', 'completed') "
            "OR (workspace_runtime_preflight_digest IS NOT NULL "
            "AND length(workspace_runtime_preflight_digest) = 64 "
            "AND workspace_runtime_preflight_digest = workspace_preflight_digest "
            "AND workspace_runtime_verified_at IS NOT NULL))",
            name="ck_execution_runs_workspace_runtime_preflight",
        ),
        Index("ix_execution_runs_active_deadline", "status", "deadline_at"),
        Index("ix_execution_runs_repository_id", "repository_id"),
        Index("ux_execution_runs_workspace_id", "workspace_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    contract_version: Mapped[int] = mapped_column(Integer, default=1)
    repository_id: Mapped[str | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="RESTRICT"), nullable=True
    )
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_workspaces.id", ondelete="RESTRICT"), nullable=True
    )
    base_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workspace_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    workspace_tree: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workspace_preflight_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    workspace_preflight_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    workspace_runtime_preflight_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    workspace_runtime_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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


class ExecutionChildRun(Base):
    __tablename__ = "execution_child_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'unknown')",
            name="ck_execution_child_runs_status",
        ),
        CheckConstraint("attempt >= 1", name="ck_execution_child_runs_attempt"),
        CheckConstraint(
            "task_fingerprint IS NULL OR length(task_fingerprint) = 64",
            name="ck_execution_child_runs_fingerprint",
        ),
        Index(
            "ux_execution_child_runs_source",
            "execution_id", "source", "source_run_id",
            unique=True,
        ),
        Index("ix_execution_child_runs_timeline", "execution_id", "started_at"),
        Index("ix_execution_child_runs_role", "execution_id", "role", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="opencode")
    source_run_id: Mapped[str] = mapped_column(String(160), nullable=False)
    parent_source_run_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    parent_call_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    role: Mapped[str] = mapped_column(String(80), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    task_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    retry_of_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_child_runs.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ExecutionEvidence(Base):
    __tablename__ = "execution_evidence"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('stage', 'message', 'tool', 'runner', 'usage', 'error', 'retry', 'review', 'artifact')",
            name="ck_execution_evidence_kind",
        ),
        CheckConstraint("attempt >= 1", name="ck_execution_evidence_attempt"),
        Index(
            "ux_execution_evidence_source_key",
            "execution_id",
            "source",
            "source_key",
            unique=True,
        ),
        Index("ix_execution_evidence_timeline", "execution_id", "occurred_at"),
        Index("ix_execution_evidence_kind", "execution_id", "kind", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    source_key: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    role: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    retry_of_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_evidence.id", ondelete="SET NULL"), nullable=True
    )
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ExecutionResultPackage(Base):
    __tablename__ = "execution_result_packages"
    __table_args__ = (
        CheckConstraint(
            "package_version IN (1, 2)", name="ck_execution_result_packages_version"
        ),
        CheckConstraint(
            "state IN ('provisional', 'final')",
            name="ck_execution_result_packages_state",
        ),
        CheckConstraint("length(package_digest) = 64", name="ck_execution_result_packages_digest"),
        CheckConstraint(
            "((state = 'final' AND finalized_at IS NOT NULL) OR "
            "(state = 'provisional' AND finalized_at IS NULL))",
            name="ck_execution_result_packages_final_state",
        ),
    )

    execution_id: Mapped[str] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), primary_key=True
    )
    package_version: Mapped[int] = mapped_column(Integer, default=2)
    state: Mapped[str] = mapped_column(String(16), default="provisional")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    package_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class RunnerJob(Base):
    __tablename__ = "runner_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', "
            "'timed_out', 'cleanup_uncertain', 'rejected')",
            name="ck_runner_jobs_status",
        ),
        CheckConstraint(
            "timeout_seconds BETWEEN 1 AND 3600",
            name="ck_runner_jobs_timeout",
        ),
        CheckConstraint(
            "length(base_commit) IN (40, 64)",
            name="ck_runner_jobs_base_commit",
        ),
        CheckConstraint(
            "length(preflight_digest) = 64",
            name="ck_runner_jobs_preflight_digest",
        ),
        CheckConstraint(
            "runner_image_id IS NULL OR length(runner_image_id) = 71",
            name="ck_runner_jobs_image_id",
        ),
        CheckConstraint(
            "length(idempotency_key) = 36",
            name="ck_runner_jobs_idempotency_key",
        ),
        CheckConstraint("lease_generation >= 0", name="ck_runner_jobs_lease_generation"),
        CheckConstraint("failure_count >= 0", name="ck_runner_jobs_failure_count"),
        CheckConstraint(
            "((lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))",
            name="ck_runner_jobs_lease_pair",
        ),
        CheckConstraint(
            "((status = 'running' AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'running' AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL))",
            name="ck_runner_jobs_active_lease",
        ),
        CheckConstraint(
            "((status = 'queued' AND next_attempt_at IS NOT NULL) OR "
            "(status <> 'queued' AND next_attempt_at IS NULL))",
            name="ck_runner_jobs_queue_state",
        ),
        CheckConstraint(
            "((status IN ('queued', 'running') AND finished_at IS NULL) OR "
            "(status NOT IN ('queued', 'running') AND finished_at IS NOT NULL))",
            name="ck_runner_jobs_terminal_time",
        ),
        CheckConstraint(
            "((status IN ('completed', 'failed', 'timed_out') "
            "AND cleanup_confirmed = TRUE AND runner_image_id IS NOT NULL) OR "
            "(status = 'cleanup_uncertain' AND cleanup_confirmed = FALSE) OR "
            "(status = 'rejected' AND cleanup_confirmed = TRUE) OR "
            "(status IN ('queued', 'running') AND cleanup_confirmed IS NULL))",
            name="ck_runner_jobs_cleanup_state",
        ),
        CheckConstraint(
            "((status IN ('completed', 'failed') AND exit_code IS NOT NULL) OR "
            "(status IN ('queued', 'running', 'timed_out', 'rejected') "
            "AND exit_code IS NULL) OR status = 'cleanup_uncertain')",
            name="ck_runner_jobs_exit_code",
        ),
        CheckConstraint(
            "source_snapshot_digest IS NULL OR length(source_snapshot_digest) = 64",
            name="ck_runner_jobs_source_snapshot_digest",
        ),
        CheckConstraint(
            "checkpoint_digest IS NULL OR length(checkpoint_digest) = 64",
            name="ck_runner_jobs_checkpoint_digest",
        ),
        CheckConstraint(
            "((checkpoint_digest IS NULL AND checkpoint_command_index IS NULL "
            "AND checkpoint_label IS NULL) OR "
            "(checkpoint_digest IS NOT NULL AND checkpoint_command_index IS NOT NULL "
            "AND checkpoint_command_index >= 0 AND source_snapshot_digest IS NOT NULL))",
            name="ck_runner_jobs_checkpoint_binding",
        ),
        Index(
            "ux_runner_jobs_execution_idempotency",
            "execution_id",
            "idempotency_key",
            unique=True,
        ),
        Index("ix_runner_jobs_queue", "status", "next_attempt_at"),
        Index("ix_runner_jobs_lease", "lease_expires_at"),
        Index("ix_runner_jobs_execution", "execution_id", "status"),
        Index("ix_runner_jobs_repository", "repository_id", "status"),
        Index(
            "ux_runner_jobs_checkpoint_command",
            "execution_id",
            "checkpoint_digest",
            "checkpoint_command_index",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    repository_id: Mapped[str] = mapped_column(
        ForeignKey("repositories.id", ondelete="RESTRICT"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("task_workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    argv: Mapped[list] = mapped_column(JSON, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    base_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    preflight_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checkpoint_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checkpoint_command_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checkpoint_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    runner_image_id: Mapped[str | None] = mapped_column(String(71), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    output_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    cleanup_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_generation: Mapped[int] = mapped_column(Integer, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
