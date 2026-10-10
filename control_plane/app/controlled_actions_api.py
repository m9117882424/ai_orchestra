from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import require_control_request, require_manager
from .controlled_actions import (
    MAX_RECONCILIATION_DETAILS_BYTES,
    ControlledActionError,
    action_digest_for_manifest,
    assert_bounded_json,
    authorization_is_expired,
    build_action_manifest,
    operation_key_for_digest,
    required_capability,
    verify_action_record,
)
from .db import get_db
from .evidence import HIGH_ASSURANCE_TIERS
from .models import (
    CapabilityGuard,
    ControlledAction,
    ControlledActionAuthorization,
    ControlledActionEffect,
    ExecutionResultPackage,
    ExecutionRun,
    Repository,
    Task,
)
from .schemas import (
    ControlledActionAuthorizationCreate,
    ControlledActionAuthorizationDecision,
    ControlledActionAuthorizationRead,
    ControlledActionClaim,
    ControlledActionCreate,
    ControlledActionEffectRead,
    ControlledActionEffectReconciliation,
    ControlledActionRead,
)
from .services import write_audit


router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
Manager = Annotated[str, Depends(require_manager)]
Mutation = Annotated[None, Depends(require_control_request)]


def _controlled_action_or_404(db: Session, action_id: str, *, lock: bool = False) -> ControlledAction:
    statement = select(ControlledAction).where(ControlledAction.id == action_id)
    if lock:
        statement = statement.with_for_update()
    action = db.scalar(statement)
    if action is None:
        raise HTTPException(status_code=404, detail="Controlled action не найден")
    try:
        verify_action_record(action)
    except ControlledActionError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return action


@router.get("/api/controlled-actions", response_model=list[ControlledActionRead])
def list_controlled_actions(
    db: DbSession,
    _: Manager,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ControlledAction]:
    actions = list(
        db.scalars(
            select(ControlledAction)
            .order_by(ControlledAction.created_at.desc(), ControlledAction.id.desc())
            .limit(limit)
        )
    )
    for action in actions:
        try:
            verify_action_record(action)
        except ControlledActionError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
    return actions


@router.get("/api/controlled-actions/{action_id}", response_model=ControlledActionRead)
def get_controlled_action(action_id: str, db: DbSession, _: Manager) -> ControlledAction:
    return _controlled_action_or_404(db, action_id)


@router.post(
    "/api/controlled-actions",
    response_model=ControlledActionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_controlled_action(
    payload: ControlledActionCreate,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> ControlledAction:
    repository = db.get(Repository, payload.repository_id)
    if repository is None:
        raise HTTPException(status_code=404, detail="Репозиторий не найден")
    if not repository.enabled or repository.status != "ready":
        raise HTTPException(status_code=409, detail="Репозиторий не готов к controlled action")

    task = None
    if payload.task_id is not None:
        task = db.get(Task, payload.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Связанная задача не найдена")
        if task.repository_id != repository.id:
            raise HTTPException(
                status_code=409,
                detail="Задача не привязана к указанному репозиторию",
            )

    if repository.assurance_tier in HIGH_ASSURANCE_TIERS and payload.result_package_digest is None:
        raise HTTPException(
            status_code=409,
            detail="High-assurance controlled action требует final Result Package digest",
        )

    if payload.result_package_digest is not None:
        package = db.scalar(
            select(ExecutionResultPackage).where(
                ExecutionResultPackage.package_digest == payload.result_package_digest
            )
        )
        if package is None:
            raise HTTPException(status_code=404, detail="Result Package не найден")
        if package.state != "final":
            raise HTTPException(status_code=409, detail="Controlled action требует final Result Package")
        run = db.get(ExecutionRun, package.execution_id)
        if run is None or run.repository_id != repository.id:
            raise HTTPException(
                status_code=409,
                detail="Result Package относится к другому репозиторию",
            )
        if task is not None and run.task_id != task.id:
            raise HTTPException(
                status_code=409,
                detail="Result Package относится к другой задаче",
            )
        if repository.assurance_tier in HIGH_ASSURANCE_TIERS:
            package_payload = package.payload if isinstance(package.payload, dict) else {}
            assurance = package_payload.get("assurance")
            if (
                not isinstance(assurance, dict)
                or assurance.get("provenance_status") != "complete"
            ):
                raise HTTPException(
                    status_code=409,
                    detail="High-assurance Result Package provenance incomplete",
                )

    try:
        manifest = build_action_manifest(**payload.model_dump())
        action_digest = action_digest_for_manifest(manifest)
    except ControlledActionError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc

    existing = db.scalar(
        select(ControlledAction).where(ControlledAction.action_digest == action_digest)
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="Exact controlled action уже существует; повторный authorization запрещён",
        )

    action = ControlledAction(
        task_id=payload.task_id,
        repository_id=repository.id,
        action_type=payload.action_type,
        source_sha=payload.source_sha,
        head_sha=payload.head_sha,
        destination=payload.destination,
        result_package_digest=payload.result_package_digest,
        payload=payload.payload,
        action_digest=action_digest,
        status="proposed",
        created_by=manager,
    )
    db.add(action)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Controlled action digest уже зарегистрирован") from exc
    write_audit(
        db,
        actor=manager,
        action="controlled_action.proposed",
        entity_type="controlled_action",
        entity_id=action.id,
        details={
            "action_type": action.action_type,
            "action_digest": action.action_digest,
            "repository_id": action.repository_id,
            "task_id": action.task_id,
            "destination": action.destination,
        },
    )
    db.commit()
    db.refresh(action)
    return action


@router.get(
    "/api/controlled-actions/{action_id}/authorizations",
    response_model=list[ControlledActionAuthorizationRead],
)
def list_controlled_action_authorizations(
    action_id: str,
    db: DbSession,
    _: Manager,
) -> list[ControlledActionAuthorization]:
    action = _controlled_action_or_404(db, action_id)
    authorizations = list(
        db.scalars(
            select(ControlledActionAuthorization)
            .where(ControlledActionAuthorization.action_id == action_id)
            .order_by(
                ControlledActionAuthorization.created_at.desc(),
                ControlledActionAuthorization.id.desc(),
            )
        )
    )
    if any(item.action_digest != action.action_digest for item in authorizations):
        raise HTTPException(status_code=409, detail="Authorization digest mismatch detected")
    return authorizations


@router.post(
    "/api/controlled-actions/{action_id}/authorizations",
    response_model=ControlledActionAuthorizationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_controlled_action_authorization(
    action_id: str,
    payload: ControlledActionAuthorizationCreate,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> ControlledActionAuthorization:
    action = _controlled_action_or_404(db, action_id, lock=True)
    if payload.expected_action_digest != action.action_digest:
        raise HTTPException(status_code=409, detail="Controlled action digest изменился")
    if action.status not in {"proposed", "approval_rejected"}:
        raise HTTPException(status_code=409, detail="Controlled action уже имеет активный lifecycle")

    now = datetime.now(timezone.utc)
    active = list(
        db.scalars(
            select(ControlledActionAuthorization)
            .where(
                ControlledActionAuthorization.action_id == action.id,
                ControlledActionAuthorization.status.in_(("pending", "approved")),
            )
            .with_for_update()
        )
    )
    for authorization in active:
        if authorization_is_expired(authorization, now=now):
            authorization.status = "expired"
            authorization.updated_at = now
        else:
            raise HTTPException(status_code=409, detail="Для action уже есть активное authorization")

    authorization = ControlledActionAuthorization(
        action_id=action.id,
        action_digest=action.action_digest,
        status="pending",
        requested_by=manager,
        reason=payload.reason,
        expires_at=now + timedelta(seconds=payload.ttl_seconds),
    )
    action.status = "pending_approval"
    action.version += 1
    action.updated_at = now
    db.add(authorization)
    db.flush()
    write_audit(
        db,
        actor=manager,
        action="controlled_action.authorization_requested",
        entity_type="controlled_action_authorization",
        entity_id=authorization.id,
        details={
            "controlled_action_id": action.id,
            "action_digest": action.action_digest,
            "expires_at": authorization.expires_at.isoformat(),
        },
    )
    db.commit()
    db.refresh(authorization)
    return authorization


@router.post(
    "/api/controlled-action-authorizations/{authorization_id}/decision",
    response_model=ControlledActionAuthorizationRead,
)
def decide_controlled_action_authorization(
    authorization_id: str,
    payload: ControlledActionAuthorizationDecision,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> ControlledActionAuthorization:
    authorization = db.scalar(
        select(ControlledActionAuthorization)
        .where(ControlledActionAuthorization.id == authorization_id)
        .with_for_update()
    )
    if authorization is None:
        raise HTTPException(status_code=404, detail="Controlled action authorization не найдено")
    action = _controlled_action_or_404(db, authorization.action_id, lock=True)
    if (
        payload.expected_action_digest != action.action_digest
        or authorization.action_digest != action.action_digest
    ):
        raise HTTPException(status_code=409, detail="Authorization больше не соответствует action digest")
    if authorization.status != "pending":
        raise HTTPException(status_code=409, detail="Authorization уже завершено")

    now = datetime.now(timezone.utc)
    if authorization_is_expired(authorization, now=now):
        authorization.status = "expired"
        authorization.updated_at = now
        action.status = "proposed"
        action.version += 1
        action.updated_at = now
        write_audit(
            db,
            actor=manager,
            action="controlled_action.authorization_expired",
            entity_type="controlled_action_authorization",
            entity_id=authorization.id,
            details={"controlled_action_id": action.id, "action_digest": action.action_digest},
        )
        db.commit()
        raise HTTPException(status_code=409, detail="Authorization истекло")

    authorization.status = payload.decision
    authorization.decided_by = manager
    authorization.decision_comment = payload.comment
    authorization.decided_at = now
    authorization.updated_at = now
    action.status = "approved" if payload.decision == "approved" else "approval_rejected"
    action.version += 1
    action.updated_at = now
    write_audit(
        db,
        actor=manager,
        action=f"controlled_action.authorization_{payload.decision}",
        entity_type="controlled_action_authorization",
        entity_id=authorization.id,
        details={
            "controlled_action_id": action.id,
            "action_digest": action.action_digest,
            "comment": payload.comment,
        },
    )
    db.commit()
    db.refresh(authorization)
    return authorization


@router.post(
    "/api/controlled-actions/{action_id}/claim",
    response_model=ControlledActionEffectRead,
    status_code=status.HTTP_201_CREATED,
)
def claim_controlled_action(
    action_id: str,
    payload: ControlledActionClaim,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> ControlledActionEffect:
    action = _controlled_action_or_404(db, action_id, lock=True)
    if payload.expected_action_digest != action.action_digest:
        raise HTTPException(status_code=409, detail="Controlled action digest изменился")
    if action.status != "approved":
        raise HTTPException(status_code=409, detail="Controlled action не находится в approved state")

    guard = db.get(CapabilityGuard, 1)
    capability = required_capability(action.action_type)
    if guard is None or not bool(getattr(guard, capability, False)):
        raise HTTPException(
            status_code=403,
            detail=f"Capability {capability} остаётся fail-closed",
        )

    authorization = db.scalar(
        select(ControlledActionAuthorization)
        .where(
            ControlledActionAuthorization.action_id == action.id,
            ControlledActionAuthorization.action_digest == action.action_digest,
            ControlledActionAuthorization.status == "approved",
        )
        .order_by(
            ControlledActionAuthorization.decided_at.desc(),
            ControlledActionAuthorization.id.desc(),
        )
        .with_for_update()
    )
    if authorization is None:
        raise HTTPException(status_code=409, detail="Approved exact-digest authorization отсутствует")

    now = datetime.now(timezone.utc)
    if authorization_is_expired(authorization, now=now):
        authorization.status = "expired"
        authorization.updated_at = now
        action.status = "proposed"
        action.version += 1
        action.updated_at = now
        write_audit(
            db,
            actor=manager,
            action="controlled_action.authorization_expired",
            entity_type="controlled_action_authorization",
            entity_id=authorization.id,
            details={"controlled_action_id": action.id, "action_digest": action.action_digest},
        )
        db.commit()
        raise HTTPException(status_code=409, detail="Authorization истекло до claim")

    operation_key = operation_key_for_digest(action.action_digest)
    if db.scalar(
        select(ControlledActionEffect).where(ControlledActionEffect.action_id == action.id)
    ) is not None:
        raise HTTPException(status_code=409, detail="Controlled action уже был claimed")
    if db.scalar(
        select(ControlledActionEffect).where(
            ControlledActionEffect.operation_key == operation_key
        )
    ) is not None:
        raise HTTPException(status_code=409, detail="Deterministic operation_key уже использован")

    authorization.status = "consumed"
    authorization.consumed_by = manager
    authorization.consumed_at = now
    authorization.operation_key = operation_key
    authorization.updated_at = now
    action.status = "claimed"
    action.version += 1
    action.updated_at = now
    effect = ControlledActionEffect(
        action_id=action.id,
        authorization_id=authorization.id,
        action_digest=action.action_digest,
        operation_key=operation_key,
        status="reserved",
        claimed_by=manager,
        claimed_at=now,
    )
    db.add(effect)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Controlled action claim replay обнаружен") from exc
    write_audit(
        db,
        actor=manager,
        action="controlled_action.claimed",
        entity_type="controlled_action_effect",
        entity_id=effect.id,
        details={
            "controlled_action_id": action.id,
            "authorization_id": authorization.id,
            "action_digest": action.action_digest,
            "operation_key": effect.operation_key,
            "external_effect_performed": False,
        },
    )
    db.commit()
    db.refresh(effect)
    return effect


def _controlled_effect_or_404(
    db: Session,
    effect_id: str,
    *,
    lock: bool = False,
) -> tuple[ControlledActionEffect, ControlledAction]:
    statement = select(ControlledActionEffect).where(ControlledActionEffect.id == effect_id)
    if lock:
        statement = statement.with_for_update()
    effect = db.scalar(statement)
    if effect is None:
        raise HTTPException(status_code=404, detail="Controlled action effect не найден")
    action = _controlled_action_or_404(db, effect.action_id, lock=lock)
    if effect.action_digest != action.action_digest:
        raise HTTPException(status_code=409, detail="Effect больше не соответствует action digest")
    try:
        expected_operation_key = operation_key_for_digest(action.action_digest)
    except ControlledActionError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    if effect.operation_key != expected_operation_key:
        raise HTTPException(status_code=409, detail="Effect operation_key mismatch detected")
    return effect, action


@router.get(
    "/api/controlled-action-effects/{effect_id}",
    response_model=ControlledActionEffectRead,
)
def get_controlled_action_effect(
    effect_id: str,
    db: DbSession,
    _: Manager,
) -> ControlledActionEffect:
    effect, _ = _controlled_effect_or_404(db, effect_id)
    return effect


@router.post(
    "/api/controlled-action-effects/{effect_id}/reconciliation",
    response_model=ControlledActionEffectRead,
)
def reconcile_controlled_action_effect(
    effect_id: str,
    payload: ControlledActionEffectReconciliation,
    db: DbSession,
    manager: Manager,
    _: Mutation,
) -> ControlledActionEffect:
    effect, action = _controlled_effect_or_404(db, effect_id, lock=True)
    if payload.operation_key != effect.operation_key:
        raise HTTPException(status_code=409, detail="operation_key не соответствует claimed effect")
    if effect.status == "reconciled":
        raise HTTPException(status_code=409, detail="Effect уже reconciled")
    try:
        assert_bounded_json(
            payload.details,
            max_bytes=MAX_RECONCILIATION_DETAILS_BYTES,
            error_code="controlled_action_reconciliation_details_too_large",
        )
    except ControlledActionError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc

    now = datetime.now(timezone.utc)
    observed = payload.observed_state_digest
    if payload.outcome == "source_state":
        if observed != action.source_sha:
            raise HTTPException(status_code=409, detail="Observed source state не совпадает с exact source SHA")
        if effect.preflight_reconciled_at is not None:
            raise HTTPException(status_code=409, detail="Preflight reconciliation уже зафиксирован")
        effect.observed_before_digest = observed
        effect.preflight_reconciled_at = now
    elif payload.outcome == "desired_state":
        if observed != action.head_sha:
            raise HTTPException(status_code=409, detail="Observed desired state не совпадает с exact head SHA")
        effect.result_digest = observed
        effect.status = "reconciled"
        effect.finished_at = effect.finished_at or now
        effect.reconciled_at = now
        action.status = "reconciled"
        action.version += 1
        action.updated_at = now
    else:
        effect.result_digest = observed
        effect.status = "uncertain"
        effect.reconciled_at = now
        action.status = "uncertain"
        action.version += 1
        action.updated_at = now

    if payload.external_ref is not None:
        effect.external_ref = payload.external_ref
    effect.details = {
        **(effect.details if isinstance(effect.details, dict) else {}),
        "last_reconciliation": {
            "outcome": payload.outcome,
            "observed_state_digest": observed,
            "recorded_at": now.isoformat(),
            "recorded_by": manager,
            "details": payload.details,
        },
    }
    effect.updated_at = now
    write_audit(
        db,
        actor=manager,
        action="controlled_action.reconciliation_recorded",
        entity_type="controlled_action_effect",
        entity_id=effect.id,
        details={
            "controlled_action_id": action.id,
            "action_digest": action.action_digest,
            "operation_key": effect.operation_key,
            "outcome": payload.outcome,
            "observed_state_digest": observed,
        },
    )
    db.commit()
    db.refresh(effect)
    return effect


