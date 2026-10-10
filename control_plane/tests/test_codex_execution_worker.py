"""Worker security checks use local files and fake Codex processes only."""
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import codex_execution_worker as worker


def token_file(tmp_path):
    directory = tmp_path / "codex-subscription-private"
    directory.mkdir(mode=0o700)
    path = directory / "one-shot.token"
    path.write_text("x" * 48)
    path.chmod(0o400)
    return path


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000@evil.invalid:80", "http://evil.invalid:80",
    "http://127.0.0.1:8000/path", "http://127.0.0.1:8000?q=1",
    "http://127.0.0.1:8000#frag", "http://127.0.0.1:0",
    "http://127.0.0.1:65536", "https://127.0.0.1:8000",
    "http://user@127.0.0.1:8000", "http://127.0.0.1:abc",
])
def test_bad_broker_url_does_not_consume_token(tmp_path, url):
    token = token_file(tmp_path)
    with pytest.raises(RuntimeError):
        worker.Broker(url, token)
    assert token.exists()


def test_one_shot_token_rejects_symlink_and_wrong_parent_mode(tmp_path):
    token = token_file(tmp_path)
    link = token.parent / "link"
    link.symlink_to(token)
    with pytest.raises(OSError):
        worker.Broker("http://127.0.0.1:8000", link)
    token.parent.chmod(0o755)
    with pytest.raises(RuntimeError):
        worker.Broker("http://127.0.0.1:8000", token)
    assert token.exists()


def test_unlink_failure_prevents_broker_start(tmp_path, monkeypatch):
    token = token_file(tmp_path)
    def denied(*args, **kwargs):
        raise PermissionError("synthetic unlink denial")
    monkeypatch.setattr(worker.os, "unlink", denied)
    with pytest.raises(PermissionError):
        worker.Broker("http://127.0.0.1:8000", token)
    assert token.exists()


def test_token_is_gone_before_model_starts(tmp_path, monkeypatch):
    token = token_file(tmp_path)
    broker = worker.Broker("http://127.0.0.1:8000", token)
    assert not token.exists()
    monkeypatch.setattr(broker, "post", lambda path, data=None: {"ok": True})
    monkeypatch.setattr(worker, "verify_runtime_workspace", lambda binding: None)
    monkeypatch.setattr(worker, "verify_chatgpt_auth", lambda codex, env: None)
    fake = tmp_path / "fake-codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"assert not pathlib.Path({str(token)!r}).exists()\n"
        "assert 'PRODUCTION_PASSWORD' not in os.environ\n"
        "sys.stdin.read()\n"
        "pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('Ж'*60000)\n"
        "print(json.dumps({'type':'turn.completed'}))\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PRODUCTION_PASSWORD", "synthetic")
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    binding = dict(execution_id="run-1", task_id="task", repository_id="repo", workspace_id="workspace",
                   base_commit="a" * 40, branch_name="main", workspace_path=str(tmp_path),
                   initial_tree="b" * 40, tracked_entries=1, preflight_digest="c" * 64)
    claim = {"execution_id": "run-1", "generation": 1, "workspace": binding,
             "title": "Review", "description": "Review", "deadline_at":
             (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()}
    result = worker.run_phase(broker, claim, "architect", output, str(fake))
    assert result["outcome"] == "completed"
    assert len(result["final_text"]) == 60000
    assert "PRODUCTION_PASSWORD" not in json.loads((output / "run-1/architect/summary.json").read_text())["final_text"]


def test_redirect_refused_without_following_external_url(tmp_path):
    broker = worker.Broker("http://127.0.0.1:8000", token_file(tmp_path))
    assert any(isinstance(handler, worker.NoRedirect) for handler in broker.opener.handlers)
    with pytest.raises(RuntimeError, match="redirect"):
        worker.NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://evil.invalid/steal")


def test_stop_group_kills_descendant_after_leader_exits(tmp_path):
    child_file = tmp_path / "pid"
    code = ("import subprocess,sys,pathlib; p=subprocess.Popen([sys.executable,'-c',"
            "'import time;time.sleep(60)']);pathlib.Path(" + repr(str(child_file)) +
            ").write_text(str(p.pid))")
    import subprocess
    process = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    process.wait(timeout=5)
    child = int(child_file.read_text())
    worker.stop_group(process)
    for _ in range(100):
        state = Path(f"/proc/{child}/stat")
        if not state.exists() or state.read_text().split()[2] == "Z":
            break
        time.sleep(0.02)
    else:
        os.kill(child, signal.SIGKILL)
        raise AssertionError("model descendant survived group cleanup")


def test_oversize_final_is_not_loaded(tmp_path):
    path = tmp_path / "final.txt"
    path.write_text("x" * (worker.MAX_FINAL_BYTES + 1))
    assert worker.read_final(path) == ("", False)


def test_sigterm_stops_active_process_group(monkeypatch):
    class Process:
        pid = 12345

    process = Process()
    calls = []
    monkeypatch.setattr(worker, "_active_process", process)
    monkeypatch.setattr(worker, "stop_group", lambda current: calls.append(current))
    with pytest.raises(SystemExit) as stopped:
        worker.interrupt_worker(signal.SIGTERM, None)
    assert stopped.value.code == 128 + signal.SIGTERM
    assert calls == [process]


def test_worker_disables_dumpability_before_model():
    import subprocess
    script = ("import ctypes;from scripts.codex_execution_worker import disable_dumpable;"
              "disable_dumpable();assert ctypes.CDLL(None).prctl(3,0,0,0,0)==0")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

def test_worker_never_starts_inference_before_preflight_ack(tmp_path, monkeypatch):
    claim = {"execution_id": "run-1", "generation": 1,
             "workspace": {"execution_id": "run-1", "task_id": "task-1",
                           "repository_id": "repo-1", "workspace_id": "workspace-1",
                           "base_commit": "a" * 40, "branch_name": "main",
                           "workspace_path": "/workspace/worktrees/managed/workspace-1",
                           "initial_tree": "b" * 40, "tracked_entries": 1,
                           "preflight_digest": "c" * 64}}
    calls = []

    class FakeBroker:
        def __init__(self, *_args):
            pass

        def post(self, path, data=None):
            calls.append((path, data))
            if path.endswith("/claim"):
                return claim
            return {"status": "running", "stage": "architect_running"}

    monkeypatch.setattr(worker, "Broker", FakeBroker)
    monkeypatch.setattr(worker.os, "geteuid", lambda: 10001)
    monkeypatch.setattr(worker.os, "umask", lambda _mask: 0o077)
    monkeypatch.setattr(sys, "argv", ["worker", "--broker", "http://127.0.0.1:8000",
                                        "--token-file", str(tmp_path / "token"),
                                        "--output", str(tmp_path / "out"), "--once"])
    monkeypatch.setattr(worker, "run_phase", lambda *_args: pytest.fail("inference before acknowledgement"))

    def failed_verification(_binding):
        raise RuntimeError("G2 verification failed")

    monkeypatch.setattr(worker, "verify_runtime_workspace", failed_verification)
    worker.main()
    assert [path for path, _ in calls] == ["/api/codex-worker/claim"]

    monkeypatch.setattr(worker, "verify_runtime_workspace", lambda _binding: calls.append(("verified", None)))
    monkeypatch.setattr(FakeBroker, "post", lambda self, path, data=None:
                        calls.append((path, data)) or
                        (claim if path.endswith("/claim") else {"status": "queued", "stage": "codex_preflight_pending"}))
    calls.clear()
    worker.main()
    assert [path for path, _ in calls] == ["/api/codex-worker/claim", "verified",
                                         "/api/codex-worker/run-1/preflight"]
    assert calls[-1][1] == {"generation": 1, "base_commit": "a" * 40,
                            "base_tree": "b" * 40, "workspace_preflight_digest": "c" * 64}
