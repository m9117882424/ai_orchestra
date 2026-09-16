from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

import runner.entrypoint as entrypoint


def binding():
    return {
        "workspace_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "base_commit": "a" * 40,
        "preflight_digest": "b" * 64,
    }


def manifest(values):
    return {
        **values,
        "workspace_path": f"/workspace/worktrees/managed/{values['workspace_id']}",
        "contract_version": 1,
    }


def test_expected_env_validates_binding(monkeypatch):
    values = binding()
    monkeypatch.setenv("AI_ORCHESTRA_RUNNER_WORKSPACE_ID", values["workspace_id"])
    monkeypatch.setenv("AI_ORCHESTRA_RUNNER_EXECUTION_ID", values["execution_id"])
    monkeypatch.setenv("AI_ORCHESTRA_RUNNER_BASE_COMMIT", values["base_commit"])
    monkeypatch.setenv("AI_ORCHESTRA_RUNNER_PREFLIGHT_DIGEST", values["preflight_digest"])
    assert entrypoint.expected_env() == values


def test_verify_manifest_accepts_exact_identity():
    values = binding()
    entrypoint.verify_manifest(manifest(values), values)


@pytest.mark.parametrize("field", ["workspace_id", "execution_id", "base_commit", "preflight_digest"])
def test_verify_manifest_rejects_mismatch(field):
    values = binding()
    payload = manifest(values)
    payload[field] = "0" * len(str(payload[field]))
    with pytest.raises(RuntimeError, match="workspace manifest mismatch"):
        entrypoint.verify_manifest(payload, values)


def test_read_manifest_rejects_symlink(tmp_path, monkeypatch):
    target = tmp_path / "manifest.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    monkeypatch.setattr(entrypoint, "MANIFEST", link)
    with pytest.raises(RuntimeError, match="manifest missing"):
        entrypoint.read_manifest()


def test_read_manifest_parses_small_regular_json(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"ok": True}), encoding="utf-8")
    monkeypatch.setattr(entrypoint, "MANIFEST", path)
    assert entrypoint.read_manifest() == {"ok": True}


def test_copy_source_uses_disposable_workspace(tmp_path, monkeypatch):
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    (source / "tracked.txt").write_text("original\n", encoding="utf-8")
    monkeypatch.setattr(entrypoint, "SOURCE", source)
    monkeypatch.setattr(entrypoint, "WORKSPACE", workspace)

    entrypoint.copy_source()
    assert (workspace / "tracked.txt").read_text(encoding="utf-8") == "original\n"
    (workspace / "tracked.txt").write_text("changed\n", encoding="utf-8")
    assert (source / "tracked.txt").read_text(encoding="utf-8") == "original\n"


def test_copy_source_rejects_nonempty_disposable_workspace(tmp_path, monkeypatch):
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    (workspace / "unexpected").write_text("x", encoding="utf-8")
    monkeypatch.setattr(entrypoint, "SOURCE", source)
    monkeypatch.setattr(entrypoint, "WORKSPACE", workspace)
    with pytest.raises(RuntimeError, match="not empty"):
        entrypoint.copy_source()


def test_expected_snapshot_digest_is_optional_and_validated(monkeypatch):
    monkeypatch.delenv("AI_ORCHESTRA_RUNNER_SOURCE_SNAPSHOT_DIGEST", raising=False)
    assert entrypoint.expected_snapshot_digest() is None
    monkeypatch.setenv("AI_ORCHESTRA_RUNNER_SOURCE_SNAPSHOT_DIGEST", "c" * 64)
    assert entrypoint.expected_snapshot_digest() == "c" * 64
    monkeypatch.setenv("AI_ORCHESTRA_RUNNER_SOURCE_SNAPSHOT_DIGEST", "c" * 63)
    with pytest.raises(RuntimeError, match="invalid source_snapshot_digest"):
        entrypoint.expected_snapshot_digest()


def test_verify_source_snapshot_detects_drift(tmp_path):
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    expected = entrypoint.source_snapshot_digest(tmp_path)
    entrypoint.verify_source_snapshot(tmp_path, expected)
    (tmp_path / "a.txt").write_text("two", encoding="utf-8")
    with pytest.raises(RuntimeError, match="runner source snapshot mismatch"):
        entrypoint.verify_source_snapshot(tmp_path, expected)
