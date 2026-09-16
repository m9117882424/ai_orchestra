from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID, uuid5

CHECKPOINT_BEGIN = "<AI_ORCHESTRA_RUNNER_CHECKPOINT>"
CHECKPOINT_END = "</AI_ORCHESTRA_RUNNER_CHECKPOINT>"
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")


class RunnerCheckpointError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RunnerCheckpointCommand:
    label: str
    argv: tuple[str, ...]
    timeout_seconds: int


@dataclass(frozen=True)
class RunnerCheckpoint:
    commands: tuple[RunnerCheckpointCommand, ...]
    canonical_json: str


def _validate_argv(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        raise RunnerCheckpointError("runner_checkpoint_argv_invalid")
    result: list[str] = []
    total = 0
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 4096 or "\x00" in item:
            raise RunnerCheckpointError("runner_checkpoint_argv_invalid")
        total += len(item.encode("utf-8"))
        if total > 32768:
            raise RunnerCheckpointError("runner_checkpoint_argv_too_large")
        result.append(item)
    return tuple(result)


def parse_runner_checkpoint(text: str) -> RunnerCheckpoint | None:
    has_begin = CHECKPOINT_BEGIN in text
    has_end = CHECKPOINT_END in text
    if not has_begin and not has_end:
        return None
    if text.count(CHECKPOINT_BEGIN) != 1 or text.count(CHECKPOINT_END) != 1:
        raise RunnerCheckpointError("runner_checkpoint_delimiter_invalid")
    before, remainder = text.split(CHECKPOINT_BEGIN, 1)
    payload_text, after = remainder.split(CHECKPOINT_END, 1)
    if before.strip() or after.strip():
        raise RunnerCheckpointError("runner_checkpoint_must_be_standalone")
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise RunnerCheckpointError("runner_checkpoint_json_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "commands"}:
        raise RunnerCheckpointError("runner_checkpoint_shape_invalid")
    if payload.get("version") != 1:
        raise RunnerCheckpointError("runner_checkpoint_version_invalid")
    commands_raw = payload.get("commands")
    if not isinstance(commands_raw, list) or not 1 <= len(commands_raw) <= 8:
        raise RunnerCheckpointError("runner_checkpoint_commands_invalid")
    commands: list[RunnerCheckpointCommand] = []
    canonical_commands: list[dict[str, object]] = []
    labels: set[str] = set()
    for item in commands_raw:
        if not isinstance(item, dict) or set(item) != {"label", "argv", "timeout_seconds"}:
            raise RunnerCheckpointError("runner_checkpoint_command_shape_invalid")
        label = item.get("label")
        timeout = item.get("timeout_seconds")
        if not isinstance(label, str) or not _LABEL_RE.fullmatch(label) or label in labels:
            raise RunnerCheckpointError("runner_checkpoint_label_invalid")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 900:
            raise RunnerCheckpointError("runner_checkpoint_timeout_invalid")
        argv = _validate_argv(item.get("argv"))
        labels.add(label)
        commands.append(RunnerCheckpointCommand(label=label, argv=argv, timeout_seconds=timeout))
        canonical_commands.append(
            {"label": label, "argv": list(argv), "timeout_seconds": timeout}
        )
    canonical = json.dumps(
        {"version": 1, "commands": canonical_commands},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return RunnerCheckpoint(commands=tuple(commands), canonical_json=canonical)

def runner_checkpoint_digest(
    execution_id: str,
    assistant_message_id: str,
    source_snapshot_digest: str,
    checkpoint: RunnerCheckpoint,
) -> str:
    try:
        parsed = UUID(execution_id)
    except ValueError as exc:
        raise RunnerCheckpointError("runner_checkpoint_execution_id_invalid") from exc
    if str(parsed) != execution_id or not assistant_message_id or len(assistant_message_id) > 160:
        raise RunnerCheckpointError("runner_checkpoint_identity_invalid")
    if len(source_snapshot_digest) != 64 or any(
        ch not in "0123456789abcdef" for ch in source_snapshot_digest
    ):
        raise RunnerCheckpointError("runner_checkpoint_snapshot_invalid")
    material = "\0".join(
        [execution_id, assistant_message_id, source_snapshot_digest, checkpoint.canonical_json]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def runner_checkpoint_idempotency_key(
    execution_id: str,
    checkpoint_digest: str,
    command_index: int,
) -> str:
    namespace = UUID(execution_id)
    return str(uuid5(namespace, f"runner-checkpoint:{checkpoint_digest}:{command_index}"))


def runner_evidence_message_ids(execution_id: str, checkpoint_digest: str) -> tuple[str, str]:
    compact = execution_id.replace("-", "")
    suffix = checkpoint_digest[:16]
    return (
        f"msg_orchestra_runner_{compact}_{suffix}",
        f"prt_orchestra_runner_{compact}_{suffix}",
    )
