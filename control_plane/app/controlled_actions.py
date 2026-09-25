from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from .models import ControlledAction, ControlledActionAuthorization


MAX_ACTION_MANIFEST_BYTES = 32 * 1024
MAX_RECONCILIATION_DETAILS_BYTES = 16 * 1024


class ControlledActionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ControlledActionError("controlled_action_manifest_not_json") from exc


def assert_bounded_json(value: object, *, max_bytes: int, error_code: str) -> None:
    if len(canonical_json(value).encode("utf-8")) > max_bytes:
        raise ControlledActionError(error_code)


def build_action_manifest(
    *,
    task_id: str | None,
    repository_id: str,
    action_type: str,
    source_sha: str,
    head_sha: str,
    destination: str,
    result_package_digest: str | None,
    payload: dict,
) -> dict:
    manifest = {
        "schema_version": 1,
        "task_id": task_id,
        "repository_id": repository_id,
        "action_type": action_type,
        "source_sha": source_sha.lower(),
        "head_sha": head_sha.lower(),
        "destination": destination.strip(),
        "result_package_digest": (
            result_package_digest.lower() if result_package_digest is not None else None
        ),
        "payload": payload,
    }
    serialized = canonical_json(manifest).encode("utf-8")
    if len(serialized) > MAX_ACTION_MANIFEST_BYTES:
        raise ControlledActionError("controlled_action_manifest_too_large")
    return manifest


def action_digest_for_manifest(manifest: dict) -> str:
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


def action_manifest_for_record(action: ControlledAction) -> dict:
    return build_action_manifest(
        task_id=action.task_id,
        repository_id=action.repository_id,
        action_type=action.action_type,
        source_sha=action.source_sha,
        head_sha=action.head_sha,
        destination=action.destination,
        result_package_digest=action.result_package_digest,
        payload=action.payload if isinstance(action.payload, dict) else {},
    )


def verify_action_record(action: ControlledAction) -> None:
    expected = action_digest_for_manifest(action_manifest_for_record(action))
    if expected != action.action_digest:
        raise ControlledActionError("controlled_action_digest_mismatch")


def operation_key_for_digest(action_digest: str) -> str:
    if len(action_digest) != 64 or any(ch not in "0123456789abcdef" for ch in action_digest):
        raise ControlledActionError("controlled_action_digest_invalid")
    return f"g5-{action_digest}"


def authorization_is_expired(
    authorization: ControlledActionAuthorization,
    *,
    now: datetime,
) -> bool:
    return as_utc(authorization.expires_at) <= as_utc(now)


def required_capability(action_type: str) -> str:
    if action_type == "deploy":
        return "production_deploy_allowed"
    return "external_write_allowed"
