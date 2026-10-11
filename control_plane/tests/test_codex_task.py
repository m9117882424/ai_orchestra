"""Exercise the operator Codex lane without a model call or paid provider."""

import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "codex_task.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _setup(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "initial")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Implement an example\n")
    fake = tmp_path / "codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "if sys.argv[1:3] == ['login', 'status']:\n"
        "    print('Logged in using ChatGPT')\n"
        "else:\n"
        "    pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('Done')\n"
        "    print(json.dumps({'type': 'turn.completed'}))\n"
    )
    fake.chmod(0o755)
    command = [sys.executable, str(SCRIPT), "--repository", str(repo), "--task-id", "taxi-001",
               "--prompt-file", str(prompt), "--worktrees", str(tmp_path / "worktrees"),
               "--output", str(tmp_path / "output"), "--codex", str(fake),
               "--role", "developer", "--allow-write"]
    env = {k: v for k, v in os.environ.items() if k not in {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"}}
    return repo, command, env


def test_codex_task_creates_isolated_worktree_and_evidence(tmp_path):
    repo, command, env = _setup(tmp_path)
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    summary = json.loads((tmp_path / "output" / "taxi-001" / "run.json").read_text())
    assert payload["branch"] == "codex/taxi-001"
    assert summary["exit_code"] == 0
    assert summary["model"] == "gpt-6-sol"
    assert summary["base_commit"] == subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    assert summary["prompt_sha256"]
    assert summary["workspace_head"] == summary["base_commit"]
    assert summary["workspace_status"] == ""
    assert summary["api_cost"] == "unknown"
    assert len(summary["events_sha256"]) == 64
    assert len(summary["final_sha256"]) == 64
    assert (tmp_path / "output" / "taxi-001" / "events.jsonl").read_text().strip() == '{"type": "turn.completed"}'
    assert (tmp_path / "output" / "taxi-001" / "final.txt").read_text() == "Done"
    assert (tmp_path / "worktrees" / "taxi-001" / "README.md").read_text() == "base\n"
    assert subprocess.run(command, text=True, capture_output=True, env=env).returncode == 2
    assert (repo / "README.md").read_text() == "base\n"


def test_codex_task_rejects_api_key_without_creating_worktree(tmp_path):
    _, command, env = _setup(tmp_path)
    env["OPENAI_API_KEY"] = "test-only"
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    assert result.returncode == 2
    assert "Unset OPENAI_API_KEY" in result.stderr
    assert not (tmp_path / "worktrees" / "taxi-001").exists()


def test_codex_task_rejects_api_key_login_mode(tmp_path):
    _, command, env = _setup(tmp_path)
    fake = Path(command[command.index("--codex") + 1])
    fake.write_text(fake.read_text().replace("Logged in using ChatGPT", "Logged in using API key"))
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    assert result.returncode == 2
    assert "signed in with ChatGPT" in result.stderr
    assert not (tmp_path / "worktrees" / "taxi-001").exists()


def test_codex_login_status_timeout_is_bounded(tmp_path):
    module = _load_runner()
    fake = tmp_path / "codex"
    fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    fake.chmod(0o755)
    import pytest
    with pytest.raises(RuntimeError, match="timed out"):
        module.verify_chatgpt_auth(str(fake), {}, timeout=0.1)


def test_codex_task_rejects_api_endpoint_override(tmp_path):
    _, command, env = _setup(tmp_path)
    env["OPENAI_BASE_URL"] = "https://synthetic-paid-provider.invalid"
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    assert result.returncode == 2
    assert "OPENAI_BASE_URL" in result.stderr
    assert not (tmp_path / "worktrees" / "taxi-001").exists()


def test_codex_task_does_not_pass_unrelated_credentials_to_cli(tmp_path):
    _, command, env = _setup(tmp_path)
    fake = Path(command[command.index("--codex") + 1])
    fake.write_text(fake.read_text().replace(
        "    pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('Done')",
        "    assert 'PRODUCTION_DB_PASSWORD' not in __import__('os').environ\n"
        "    pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('Done')"))
    env["PRODUCTION_DB_PASSWORD"] = "synthetic-secret"
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    assert result.returncode == 0, result.stderr


def test_codex_task_write_requires_explicit_developer_role(tmp_path):
    _, command, env = _setup(tmp_path)
    no_policy = [arg for arg in command if arg != "--allow-write"]
    assert subprocess.run(no_policy, capture_output=True, env=env).returncode == 2
    read_only = command.copy()
    read_only[read_only.index("developer")] = "reviewer"
    assert subprocess.run(read_only, capture_output=True, env=env).returncode == 2
    assert not (tmp_path / "worktrees" / "taxi-001").exists()


def test_codex_read_only_role_detects_workspace_mutation(tmp_path):
    _, command, env = _setup(tmp_path)
    command = [arg for arg in command if arg != "--allow-write"]
    command[command.index("developer")] = "qa"
    fake = Path(command[command.index("--codex") + 1])
    fake.write_text(fake.read_text().replace(
        "    pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('Done')",
        "    pathlib.Path(sys.argv[sys.argv.index('--cd') + 1], 'README.md').write_text('changed')\n"
        "    pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('Done')"))
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    assert result.returncode == 66
    summary = json.loads((tmp_path / "output/taxi-001/run.json").read_text())
    assert summary["outcome"] == "read_only_violation"
    assert summary["workspace_status"]
    assert (tmp_path / "output/taxi-001/events.jsonl").exists()


def test_codex_developer_changes_are_recorded_without_changing_base(tmp_path):
    repo, command, env = _setup(tmp_path)
    fake = Path(command[command.index("--codex") + 1])
    fake.write_text(fake.read_text().replace(
        "    pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('Done')",
        "    pathlib.Path(sys.argv[sys.argv.index('--cd') + 1], 'README.md').write_text('changed')\n"
        "    pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('Done')"))
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    assert result.returncode == 0
    summary = json.loads((tmp_path / "output/taxi-001/run.json").read_text())
    assert summary["outcome"] == "completed"
    assert "README.md" in summary["workspace_status"]
    assert summary["workspace_head"] == summary["base_commit"]
    assert (repo / "README.md").read_text() == "base\n"


def test_codex_model_failure_records_evidence_without_retry(tmp_path):
    _, command, env = _setup(tmp_path)
    fake = Path(command[command.index("--codex") + 1])
    fake.write_text(fake.read_text().replace(
        "    pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('Done')",
        "    print(json.dumps({'type': 'turn.failed', 'error': 'model unavailable'}))\n"
        "    print('model unavailable', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "    pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('Done')"))
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    assert result.returncode == 1
    summary = json.loads((tmp_path / "output/taxi-001/run.json").read_text())
    assert summary["outcome"] == "execution_failed"
    assert summary["final_received"] is False
    assert "model unavailable" in summary["error"]
    assert (tmp_path / "output/taxi-001/events.jsonl").exists()
    assert subprocess.run(command, capture_output=True, env=env).returncode == 2


def test_codex_task_rejects_dirty_repository(tmp_path):
    repo, command, env = _setup(tmp_path)
    (repo / "README.md").write_text("modified\n")
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    assert result.returncode == 2
    assert "uncommitted changes" in result.stderr
    assert not (tmp_path / "worktrees" / "taxi-001").exists()


def _load_runner():
    import importlib.util
    spec = importlib.util.spec_from_file_location("codex_task_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_codex_task_rejects_zero_exit_without_final(tmp_path):
    _, command, env = _setup(tmp_path)
    fake = Path(command[command.index("--codex") + 1])
    text = fake.read_text()
    text = text.replace("    pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('Done')", "    pass")
    fake.write_text(text)
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    assert result.returncode == 65
    summary = json.loads((tmp_path / "output/taxi-001/run.json").read_text())
    assert summary["outcome"] == "incomplete_result"
    assert summary["final_received"] is False
    assert summary["turn_completed"] is True


def test_codex_task_rejects_zero_exit_without_completed_turn(tmp_path):
    _, command, env = _setup(tmp_path)
    fake = Path(command[command.index("--codex") + 1])
    fake.write_text(fake.read_text().replace("'turn.completed'", "'turn.started'"))
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    assert result.returncode == 65
    summary = json.loads((tmp_path / "output/taxi-001/run.json").read_text())
    assert summary["outcome"] == "incomplete_result"
    assert summary["final_received"] is True


def test_codex_completed_events_fail_closed(tmp_path):
    module = _load_runner()
    events = tmp_path / "events.jsonl"
    events.write_text('[]\ninvalid-json\n{"type":"turn.completed"}\n')
    assert not module.has_completed_turn(events)
    events.write_text('{"type":"turn.completed"}\n{"type":"turn.failed"}\n')
    assert not module.has_completed_turn(events)
    events.write_text('{"type":"error"}\n{"type":"turn.completed"}\n')
    assert not module.has_completed_turn(events)


def test_codex_bounded_process_timeout_stops_descendants(tmp_path):
    import time
    module = _load_runner()
    pidfile = tmp_path / "child.pid"
    child = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
    parent = ("import subprocess,sys,time,pathlib;"
              "p=subprocess.Popen([sys.executable,'-c'," + repr(child) + "]);"
              "pathlib.Path(" + repr(str(pidfile)) + ").write_text(str(p.pid));time.sleep(60)")
    events, errors = tmp_path / "events", tmp_path / "errors"
    started = time.monotonic()
    code, error, outcome = module.execute_bounded([sys.executable, "-c", parent], "test", events, errors, 0.5)
    assert (code, outcome) == (124, "local_timeout")
    assert "process group stopped" in error
    assert time.monotonic() - started < 6
    assert events.stat().st_mode & 0o777 == 0o600
    child_pid = int(pidfile.read_text())
    stat = Path(f"/proc/{child_pid}/stat")
    for _ in range(50):
        if not stat.exists() or stat.read_text().split()[2] == "Z":
            break
        time.sleep(0.02)
    else:
        os.kill(child_pid, 9)
        raise AssertionError("Child survived the deadline")


def test_codex_bounded_process_preserves_partial_work_and_logs(tmp_path):
    module = _load_runner()
    artifact = tmp_path / "partial.txt"
    program = ("import pathlib,time,sys;pathlib.Path(" + repr(str(artifact)) +
               ").write_text('checkpoint');print('partial-event',flush=True);"
               "print('diagnostic',file=sys.stderr,flush=True);time.sleep(60)")
    events, errors = tmp_path / "events", tmp_path / "errors"
    code, error, outcome = module.execute_bounded([sys.executable, "-c", program], "", events, errors, 0.3)
    assert (code, outcome) == (124, "local_timeout")
    assert artifact.read_text() == "checkpoint"
    assert events.read_text().strip() == "partial-event"
    assert "diagnostic" in error


def test_codex_guard_failure_not_retried_or_classified_as_timeout(tmp_path):
    module = _load_runner()
    program = "import sys;print('NO_CREDIT_BLOCKED',file=sys.stderr);sys.exit(78)"
    code, error, outcome = module.execute_bounded([sys.executable, "-c", program], "", tmp_path / "events", tmp_path / "errors", 1)
    assert (code, outcome) == (78, "no_credit_guard_blocked")
    assert "NO_CREDIT_BLOCKED" in error


def test_micro_task_defaults_and_explicit_bounds(tmp_path):
    import importlib.util
    path = SCRIPT.with_name("orchestra_task_entrypoint.py")
    spec = importlib.util.spec_from_file_location("micro_entrypoint", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.micro_timeout([]) == 420
    for role in ("architect", "qa", "reviewer"):
        assert module.micro_timeout(["--role", role]) == 300
    assert module.micro_timeout(["--role", "developer", "--timeout=120"]) == 120
    for timeout in (0, 59, 601, 900):
        try:
            module.micro_timeout(["--timeout", str(timeout)])
        except ValueError:
            pass
        else:
            raise AssertionError("Unbounded task accepted")
