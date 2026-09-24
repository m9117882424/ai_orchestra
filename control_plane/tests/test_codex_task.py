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
               "--output", str(tmp_path / "output"), "--codex", str(fake)]
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


def test_codex_task_rejects_dirty_repository(tmp_path):
    repo, command, env = _setup(tmp_path)
    (repo / "README.md").write_text("modified\n")
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    assert result.returncode == 2
    assert "uncommitted changes" in result.stderr
    assert not (tmp_path / "worktrees" / "taxi-001").exists()
