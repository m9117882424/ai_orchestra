from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import AuditEvent, Budget, TradingGuard, UsageEvent


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


def seed_defaults(db: Session, default_monthly_budget: float) -> None:
    if db.get(TradingGuard, 1) is None:
        db.add(TradingGuard(id=1))

    defaults = {
        "department": Decimal(str(default_monthly_budget)),
        "trading-research": Decimal(str(default_monthly_budget)) / Decimal("4"),
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
