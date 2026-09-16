from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .repository_policy import (
    normalize_profile_reference,
    normalize_repository_name,
    normalize_repository_remote,
    validate_assurance_configuration,
)


TaskDomain = Literal["development", "analytics", "trading"]
TaskPriority = Literal["low", "normal", "high", "critical"]
TaskStatus = Literal[
    "backlog", "planned", "in_progress", "waiting_approval", "qa", "done", "failed"
]
RiskLevel = Literal["low", "medium", "high", "critical"]
ApprovalKind = Literal[
    "code_change",
    "git_push",
    "deploy",
    "secret_access",
    "external_write",
    "financial_execution",
]
ApprovalDecision = Literal["approved", "rejected"]
RepositoryProvider = Literal["github", "gitlab", "bitbucket", "generic"]
RepositoryStatus = Literal[
    "pending_validation", "validating", "ready", "unavailable", "invalid"
]
ExecutionProfile = Literal["development"]
AssuranceTier = Literal[
    "general-standard", "general-high-assurance", "regulated-critical"
]


class RepositoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    remote_url: str = Field(min_length=1, max_length=1024)
    auth_profile_ref: str | None = None
    enabled: bool = Field(default=True, strict=True)
    execution_profile: ExecutionProfile = "development"
    assurance_tier: AssuranceTier = "general-standard"
    assurance_profile: str | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_repository_name(value)

    @field_validator("remote_url")
    @classmethod
    def normalize_remote(cls, value: str) -> str:
        return normalize_repository_remote(value).url

    @field_validator("auth_profile_ref")
    @classmethod
    def normalize_auth_profile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_profile_reference(value, field_name="auth_profile_ref")

    @field_validator("assurance_profile")
    @classmethod
    def normalize_assurance_profile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_profile_reference(value, field_name="assurance_profile")

    @model_validator(mode="after")
    def validate_assurance(self) -> "RepositoryCreate":
        validate_assurance_configuration(self.assurance_tier, self.assurance_profile)
        return self


class RepositoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, strict=True)
    auth_profile_ref: str | None = None
    enabled: bool | None = Field(default=None, strict=True)
    execution_profile: ExecutionProfile | None = None
    assurance_tier: AssuranceTier | None = None
    assurance_profile: str | None = None

    @field_validator("auth_profile_ref")
    @classmethod
    def normalize_auth_profile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_profile_reference(value, field_name="auth_profile_ref")

    @field_validator("assurance_profile")
    @classmethod
    def normalize_assurance_profile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_profile_reference(value, field_name="assurance_profile")

    @model_validator(mode="after")
    def require_change(self) -> "RepositoryUpdate":
        changed_fields = self.model_fields_set - {"expected_version"}
        if not changed_fields:
            raise ValueError("Repository update не содержит изменений")
        for field_name in ("enabled", "execution_profile", "assurance_tier"):
            if field_name in changed_fields and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} нельзя установить в null")
        return self


class RepositoryValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, strict=True)


class RepositoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    remote_url: str
    remote_host: str
    provider: RepositoryProvider
    auth_profile_ref: str | None
    default_branch: str | None
    enabled: bool
    status: RepositoryStatus
    last_known_commit: str | None
    last_fetched_at: datetime | None
    sync_failure_count: int
    sync_requested_at: datetime | None
    sync_started_at: datetime | None
    sync_finished_at: datetime | None
    sync_next_at: datetime | None
    last_sync_error_code: str | None
    execution_profile: ExecutionProfile
    assurance_tier: AssuranceTier
    assurance_profile: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=200)
    description: str = Field(default="", max_length=10000)
    project: str = Field(default="general", min_length=1, max_length=120)
    repository_id: str | None = Field(default=None, min_length=36, max_length=36)
    domain: TaskDomain = "development"
    priority: TaskPriority = "normal"
    risk_level: RiskLevel = "low"
    owner_role: str | None = Field(default=None, max_length=80)


class TaskStatusUpdate(BaseModel):
    status: TaskStatus


class TaskRepositoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: str | None = Field(default=None, min_length=36, max_length=36)


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


class CapabilityGuardRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    production_deploy_allowed: bool
    external_write_allowed: bool
    financial_execution_allowed: bool
    secret_access_allowed: bool
    updated_at: datetime


ExecutionStatus = Literal[
    "preparing", "queued", "running", "completed", "failed", "cancelled"
]

WorkspaceStatus = Literal[
    "pending",
    "preparing",
    "unavailable",
    "ready",
    "inspection_pending",
    "inspecting",
    "retained",
    "cleanup_pending",
    "cleaning",
    "removed",
    "invalid",
]


class ExecutionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    contract_version: int
    repository_id: str | None
    workspace_id: str | None
    base_commit: str | None
    workspace_path: str | None
    workspace_tree: str | None
    workspace_preflight_digest: str | None
    workspace_preflight_completed_at: datetime | None
    workspace_runtime_preflight_digest: str | None
    workspace_runtime_verified_at: datetime | None
    status: ExecutionStatus
    stage: str
    opencode_session_id: str | None
    lead_role: str
    assigned_roles: list[str]
    result: str
    error: str
    lease_generation: int
    heartbeat_at: datetime | None
    deadline_at: datetime | None
    cancel_requested_at: datetime | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class WorkspaceCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, strict=True)


class TaskWorkspaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    repository_id: str
    status: WorkspaceStatus
    base_commit: str
    base_branch: str
    branch_name: str
    opencode_path: str
    initial_tree: str | None
    preflight_digest: str | None
    tracked_entries: int | None
    current_head_commit: str | None
    current_tree: str | None
    change_digest: str | None
    has_changes: bool | None
    changed_file_count: int | None
    generation: int
    failure_count: int
    requested_at: datetime
    started_at: datetime | None
    prepared_at: datetime | None
    inspection_requested_at: datetime | None
    inspected_at: datetime | None
    cleanup_requested_at: datetime | None
    cleaned_at: datetime | None
    next_attempt_at: datetime | None
    last_error_code: str | None
    version: int
    created_at: datetime
    updated_at: datetime


RunnerJobStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "timed_out",
    "cleanup_uncertain",
    "rejected",
]


class RunnerJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: UUID
    argv: list[str] = Field(min_length=1, max_length=128)
    timeout_seconds: int = Field(default=300, ge=1, le=3600, strict=True)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        total = 0
        for item in value:
            if not item or len(item) > 4096 or "\x00" in item:
                raise ValueError("argv contains an invalid argument")
            total += len(item.encode("utf-8"))
        if total > 32768:
            raise ValueError("argv is too large")
        return value


class RunnerJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    execution_id: str
    repository_id: str
    workspace_id: str
    idempotency_key: str
    status: RunnerJobStatus
    argv: list[str]
    timeout_seconds: int
    base_commit: str
    preflight_digest: str
    source_snapshot_digest: str | None
    checkpoint_digest: str | None
    checkpoint_command_index: int | None
    checkpoint_label: str | None
    runner_image_id: str | None
    exit_code: int | None
    stdout: str
    stderr: str
    output_truncated: bool
    cleanup_confirmed: bool | None
    lease_generation: int
    heartbeat_at: datetime | None
    failure_count: int
    last_error_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


class ExecutionProgressItem(BaseModel):
    role: str
    model: str | None = None
    text: str
    created_at: datetime | None = None


class ExecutionProgressRead(BaseModel):
    execution_id: str
    status: ExecutionStatus
    stage: str
    session_state: str
    elapsed_seconds: int
    heartbeat_at: datetime | None
    deadline_at: datetime | None
    cancel_requested_at: datetime | None
    lease_generation: int
    error: str
    items: list[ExecutionProgressItem]
