from __future__ import annotations

import io
from pathlib import Path
from uuid import uuid4

import pytest
import runner.runnerd as runnerd

from runner.runnerd import (
    BoundedCapture,
    DockerRunner,
    RunRequest,
    RunnerConfig,
    build_docker_command,
    parse_run_request,
)

IMAGE_ID = "sha256:" + "a" * 64
PROFILE_IMAGE_ID = "sha256:" + "d" * 64
COMMIT = "b" * 40
DIGEST = "c" * 64


def config(**overrides) -> RunnerConfig:
    values = dict(
        socket_path=Path("/tmp/runnerd.sock"),
        workspace_volume="test-workspaces",
        image_id=IMAGE_ID,
        max_timeout_seconds=300,
    )
    values.update(overrides)
    return RunnerConfig(**values)


def payload() -> dict:
    return {
        "version": 1,
        "operation": "run",
        "request_id": str(uuid4()),
        "repository_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "base_commit": COMMIT,
        "preflight_digest": DIGEST,
        "argv": ["python3", "-c", "print('ok')"],
        "timeout_seconds": 60,
    }


def test_parse_run_request_accepts_immutable_binding():
    raw = payload()
    request = parse_run_request(raw, config())
    assert request.workspace_id == raw["workspace_id"]
    assert request.execution_id == raw["execution_id"]
    assert request.base_commit == COMMIT
    assert request.preflight_digest == DIGEST
    assert request.argv[0] == "python3"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository_id", "not-a-uuid"),
        ("workspace_id", "not-a-uuid"),
        ("execution_id", "not-a-uuid"),
        ("base_commit", "deadbeef"),
        ("preflight_digest", "f" * 63),
        ("timeout_seconds", 301),
    ],
)
def test_parse_run_request_rejects_invalid_binding(field, value):
    raw = payload()
    raw[field] = value
    with pytest.raises(ValueError):
        parse_run_request(raw, config())


def test_parse_run_request_rejects_unknown_fields():
    raw = payload()
    raw["host_path"] = "/etc"
    with pytest.raises(ValueError, match="unsupported request fields"):
        parse_run_request(raw, config())


def test_repository_specific_image_is_selected_only_from_host_config():
    raw = payload()
    selected = config(repository_image_ids=((raw["repository_id"], PROFILE_IMAGE_ID),))
    request = parse_run_request(raw, selected)
    assert request.image_id == PROFILE_IMAGE_ID
    command = build_docker_command(selected, request)
    assert PROFILE_IMAGE_ID in command
    assert IMAGE_ID not in command


def test_unmapped_repository_uses_generic_default_image():
    raw = payload()
    selected = config(repository_image_ids=((str(uuid4()), PROFILE_IMAGE_ID),))
    request = parse_run_request(raw, selected)
    assert request.image_id == IMAGE_ID


def test_runner_config_parses_trusted_repository_image_map(monkeypatch, tmp_path):
    repository_id = str(uuid4())
    monkeypatch.setenv("RUNNERD_SOCKET_PATH", str(tmp_path / "runnerd.sock"))
    monkeypatch.setenv("RUNNERD_WORKSPACE_VOLUME", "test-workspaces")
    monkeypatch.setenv("RUNNERD_IMAGE_ID", IMAGE_ID)
    monkeypatch.setenv(
        "RUNNERD_REPOSITORY_IMAGE_MAP",
        f'{{"{repository_id}":"{PROFILE_IMAGE_ID}"}}',
    )
    parsed = RunnerConfig.from_env()
    assert parsed.image_id_for(repository_id) == PROFILE_IMAGE_ID
    assert parsed.image_id_for(str(uuid4())) == IMAGE_ID
    assert set(parsed.all_image_ids()) == {IMAGE_ID, PROFILE_IMAGE_ID}


@pytest.mark.parametrize(
    "mapping",
    [
        "[]",
        '{"not-a-uuid":"sha256:' + "d" * 64 + '"}',
        '{"00000000-0000-0000-0000-000000000000":"not-a-digest"}',
    ],
)
def test_runner_config_rejects_invalid_repository_image_map(monkeypatch, tmp_path, mapping):
    monkeypatch.setenv("RUNNERD_SOCKET_PATH", str(tmp_path / "runnerd.sock"))
    monkeypatch.setenv("RUNNERD_WORKSPACE_VOLUME", "test-workspaces")
    monkeypatch.setenv("RUNNERD_IMAGE_ID", IMAGE_ID)
    monkeypatch.setenv("RUNNERD_REPOSITORY_IMAGE_MAP", mapping)
    with pytest.raises(ValueError, match="RUNNERD_REPOSITORY_IMAGE_MAP"):
        RunnerConfig.from_env()


def test_request_cannot_select_image_or_profile():
    for field in ("image", "image_id", "runner_profile"):
        raw = payload()
        raw[field] = PROFILE_IMAGE_ID
        with pytest.raises(ValueError, match="unsupported request fields"):
            parse_run_request(raw, config())


def test_docker_command_has_hard_security_boundary():
    raw = payload()
    request = parse_run_request(raw, config())
    command = build_docker_command(config(), request)
    joined = " ".join(command)

    assert "--network none" in joined
    assert "--read-only" in command
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges:true" in command
    assert "--user 10001:10001" in joined
    assert "--pids-limit 128" in joined
    assert "--memory 1g" in joined
    assert "--cpus 1.0" in joined
    assert "volume-subpath=" + raw["workspace_id"] in joined
    assert "dst=/source" in joined
    assert "readonly" in joined
    assert "/workspace:rw,nosuid,nodev,size=536870912" in joined
    assert "/var/run/docker.sock" not in joined
    assert "/root" not in joined


def test_docker_command_binds_manifest_identity_and_immutable_image():
    raw = payload()
    request = parse_run_request(raw, config())
    command = build_docker_command(config(), request)
    joined = "\n".join(command)

    assert f"AI_ORCHESTRA_RUNNER_WORKSPACE_ID={raw['workspace_id']}" in joined
    assert f"AI_ORCHESTRA_RUNNER_EXECUTION_ID={raw['execution_id']}" in joined
    assert f"AI_ORCHESTRA_RUNNER_BASE_COMMIT={COMMIT}" in joined
    assert f"AI_ORCHESTRA_RUNNER_PREFLIGHT_DIGEST={DIGEST}" in joined
    assert IMAGE_ID in command
    assert "--pull" in command and "never" in command


def test_bounded_capture_drains_but_truncates_output():
    capture = BoundedCapture(8)
    capture.drain(io.BytesIO(b"0123456789abcdef"))
    assert capture.text() == "01234567"
    assert capture.truncated is True


def test_cleanup_requires_confirmed_container_absence(monkeypatch):
    calls = []

    class Result:
        def __init__(self, returncode=0, stderr="", stdout=""):
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = stdout

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "inspect" in argv:
            return Result(returncode=1, stderr="Error: No such object: runner")
        return Result(returncode=0)

    monkeypatch.setattr(runnerd.subprocess, "run", fake_run)
    runner = DockerRunner(config())
    assert runner.cleanup(str(uuid4())) is True
    assert len(calls) == 2


def test_cleanup_fails_closed_on_ambiguous_docker_error(monkeypatch):
    class Result:
        returncode = 1
        stderr = "Cannot connect to the Docker daemon"
        stdout = ""

    monkeypatch.setattr(runnerd.subprocess, "run", lambda *a, **k: Result())
    assert DockerRunner(config()).cleanup(str(uuid4())) is False


def test_snapshot_digest_is_optional_for_legacy_jobs_but_bound_when_present():
    raw = payload()
    legacy = parse_run_request(raw, config())
    assert legacy.source_snapshot_digest is None
    raw["source_snapshot_digest"] = "d" * 64
    request = parse_run_request(raw, config())
    assert request.source_snapshot_digest == "d" * 64
    command = build_docker_command(config(), request)
    joined = "\n".join(command)
    assert "AI_ORCHESTRA_RUNNER_SOURCE_SNAPSHOT_DIGEST=" + "d" * 64 in joined


def test_snapshot_digest_rejects_invalid_value():
    raw = payload()
    raw["source_snapshot_digest"] = "d" * 63
    with pytest.raises(ValueError, match="source_snapshot_digest"):
        parse_run_request(raw, config())
