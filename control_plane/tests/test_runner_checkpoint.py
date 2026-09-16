from uuid import uuid4

import pytest

from control_plane.app.runner_checkpoint import (
    CHECKPOINT_BEGIN,
    CHECKPOINT_END,
    RunnerCheckpointError,
    parse_runner_checkpoint,
    runner_checkpoint_digest,
    runner_checkpoint_idempotency_key,
    runner_evidence_message_ids,
)


def _text(payload: str) -> str:
    return f"{CHECKPOINT_BEGIN}\n{payload}\n{CHECKPOINT_END}"


def test_no_checkpoint_returns_none():
    assert parse_runner_checkpoint("normal final answer") is None


def test_valid_checkpoint_is_canonical_and_deterministic():
    text = _text(
        '{"commands":[{"timeout_seconds":30,"argv":["python3","-m","pytest"],"label":"tests"}],"version":1}'
    )
    checkpoint = parse_runner_checkpoint(text)
    assert checkpoint is not None
    assert checkpoint.commands[0].label == "tests"
    assert checkpoint.commands[0].argv == ("python3", "-m", "pytest")
    assert checkpoint.canonical_json == '{"commands":[{"argv":["python3","-m","pytest"],"label":"tests","timeout_seconds":30}],"version":1}'
    execution_id = str(uuid4())
    digest = runner_checkpoint_digest(execution_id, "msg_123", "a" * 64, checkpoint)
    assert len(digest) == 64
    assert digest == runner_checkpoint_digest(execution_id, "msg_123", "a" * 64, checkpoint)
    key = runner_checkpoint_idempotency_key(execution_id, digest, 0)
    assert key == runner_checkpoint_idempotency_key(execution_id, digest, 0)
    assert key != runner_checkpoint_idempotency_key(execution_id, digest, 1)
    message_id, part_id = runner_evidence_message_ids(execution_id, digest)
    assert message_id.startswith("msg_orchestra_runner_")
    assert part_id.startswith("prt_orchestra_runner_")


def test_checkpoint_must_be_standalone_and_unique():
    valid = _text('{"version":1,"commands":[{"label":"x","argv":["true"],"timeout_seconds":1}]}')
    with pytest.raises(RunnerCheckpointError, match="runner_checkpoint_must_be_standalone"):
        parse_runner_checkpoint("prefix\n" + valid)
    with pytest.raises(RunnerCheckpointError, match="runner_checkpoint_delimiter_invalid"):
        parse_runner_checkpoint(valid + "\n" + valid)


def test_checkpoint_rejects_duplicate_label_bool_timeout_and_extra_fields():
    duplicate = _text('{"version":1,"commands":[{"label":"x","argv":["true"],"timeout_seconds":1},{"label":"x","argv":["true"],"timeout_seconds":1}]}')
    with pytest.raises(RunnerCheckpointError, match="runner_checkpoint_label_invalid"):
        parse_runner_checkpoint(duplicate)
    bad_timeout = _text('{"version":1,"commands":[{"label":"x","argv":["true"],"timeout_seconds":true}]}')
    with pytest.raises(RunnerCheckpointError, match="runner_checkpoint_timeout_invalid"):
        parse_runner_checkpoint(bad_timeout)
    extra = _text('{"version":1,"commands":[{"label":"x","argv":["true"],"timeout_seconds":1,"shell":true}]}')
    with pytest.raises(RunnerCheckpointError, match="runner_checkpoint_command_shape_invalid"):
        parse_runner_checkpoint(extra)


def test_checkpoint_digest_binds_message_and_snapshot():
    checkpoint = parse_runner_checkpoint(
        _text('{"version":1,"commands":[{"label":"x","argv":["true"],"timeout_seconds":1}]}')
    )
    assert checkpoint is not None
    execution_id = str(uuid4())
    one = runner_checkpoint_digest(execution_id, "msg_a", "1" * 64, checkpoint)
    two = runner_checkpoint_digest(execution_id, "msg_b", "1" * 64, checkpoint)
    three = runner_checkpoint_digest(execution_id, "msg_a", "2" * 64, checkpoint)
    assert len({one, two, three}) == 3
