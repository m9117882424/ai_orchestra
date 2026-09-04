from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import AuditEvent, Budget, CapabilityGuard, UsageEvent
from .settings import get_settings


def write_audit(
    db: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict | None = None,
) -> None:
    db.add(
        AuditEvent(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
        )
    )


def _assert_runtime_schema_shape(db: Session) -> None:
    if get_settings().environment.lower() == "test":
        return
    from .schema import legacy_schema_diff

    differences = legacy_schema_diff(db.get_bind())
    if differences:
        joined = "\n - ".join(differences)
        raise RuntimeError(
            "Control Plane schema drift detected despite valid revision:\n - " + joined
        )


def seed_defaults(db: Session, default_monthly_budget: float) -> None:
    # Startup seed is also the point where all ORM models are already registered.
    # Verify physical schema shape before the application performs normal writes.
    _assert_runtime_schema_shape(db)

    if db.get(CapabilityGuard, 1) is None:
        db.add(CapabilityGuard(id=1))

    defaults = {
        "department": Decimal(str(default_monthly_budget)),
        "high-risk-research": Decimal(str(default_monthly_budget)) / Decimal("4"),
    }
    for scope, limit in defaults.items():
        if db.get(Budget, scope) is None:
            db.add(Budget(scope=scope, monthly_limit=limit, warning_pct=80, hard_stop=True))
    db.commit()


def current_month_cost(db: Session) -> Decimal:
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    value = db.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost), 0)).where(UsageEvent.created_at >= start)
    )
    return Decimal(str(value or 0))
