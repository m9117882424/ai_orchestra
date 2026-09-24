#!/usr/bin/env python3
"""Run a development task in a dedicated Git worktree using ChatGPT Codex auth.

This is an operator-invoked lane. It deliberately does not claim a control-plane
execution lease or write to execution_runs: those belong to the OpenCode worker.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


TASK_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
ROLES = {"architect": "read-only", "developer": "workspace-write", "qa": "read-only", "reviewer": "read-only"}
DEFAULT_MODELS = {"architect": "gpt-6-astra", "developer": "gpt-6-sol", "qa": "gpt-6-sol", "reviewer": "gpt-6-astra"}


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def verify_chatgpt_auth(codex: str) -> None:
    # Avoid silently billing an API account if the operator's environment
    # overrides a stored ChatGPT login.
    forbidden = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")
    present = [key for key in forbidden if os.environ.get(key)]
    if present:
        raise RuntimeError(f"Unset {', '.join(present)} before using the Pro lane")
    result = subprocess.run([codex, "login", "status"], capture_output=True, text=True)
    status = (result.stdout + "\n" + result.stderr).lower()
    if result.returncode != 0 or "chatgpt" not in status or "api key" in status:
        raise RuntimeError("Codex must be signed in with ChatGPT (run codex login --device-auth)")


def run(args: argparse.Namespace) -> int:
    if not TASK_ID.fullmatch(args.task_id):
        raise ValueError("task-id must contain only letters, digits, _ or -")
    repository = args.repository.resolve(strict=True)
    root = args.worktrees.resolve()
    output_root = args.output.resolve()
    prompt = args.prompt_file.resolve(strict=True).read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError("Prompt file is empty")
    if git(repository, "rev-parse", "--show-toplevel") != str(repository):
        raise ValueError("repository must point to the Git top-level directory")
    if git(repository, "status", "--porcelain"):
        raise ValueError("repository has uncommitted changes; preserve them before creating a task")
    base = git(repository, "rev-parse", "--verify", f"{args.base}^{{commit}}")
    worktree = root / args.task_id
    branch = f"codex/{args.task_id}"
    task_output = output_root / args.task_id
    if worktree.exists() or task_output.exists() or git(repository, "branch", "--list", branch):
        raise ValueError("Task worktree or branch already exists; use a new task-id")

    root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)
    os.chmod(output_root, 0o700)
    lock = output_root / ".codex-task.lock"
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        # Recheck after acquiring the lock to prevent two launches with one id.
        if worktree.exists() or task_output.exists() or git(repository, "branch", "--list", branch):
            raise ValueError("Task already exists")
        verify_chatgpt_auth(args.codex)
        git(repository, "worktree", "add", "-b", branch, str(worktree), base)

    task_output.mkdir(mode=0o700)
    events = task_output / "events.jsonl"
    final = task_output / "final.txt"
    summary = task_output / "run.json"
    command = [
        args.codex, "exec", "--json", "--ephemeral", "--ignore-user-config",
        "--sandbox", ROLES[args.role], "-c", "approval_policy=never",
        "--model", args.model or DEFAULT_MODELS[args.role],
        "--cd", str(worktree), "--output-last-message", str(final), "-",
    ]
    started = datetime.now(timezone.utc).isoformat()
    # Codex may return nonzero after making changes. Preserve the worktree and
    # evidence for review; never retry inference or switch to a paid provider.
    with events.open("x", encoding="utf-8") as stream:
        os.chmod(events, 0o600)
        try:
            completed = subprocess.run(command, input=prompt, text=True, stdout=stream,
                                       stderr=subprocess.PIPE, timeout=args.timeout)
            exit_code, error = completed.returncode, completed.stderr[-4000:]
        except subprocess.TimeoutExpired as exc:
            exit_code, error = 124, f"Codex timed out after {args.timeout}s: {exc}"
    payload = {
        "task_id": args.task_id, "role": args.role, "model": args.model or DEFAULT_MODELS[args.role],
        "repository": str(repository), "worktree": str(worktree), "branch": branch,
        "base_commit": base, "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(), "exit_code": exit_code,
        "error": error, "events": str(events), "final": str(final),
    }
    summary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(summary, 0o600)
    print(json.dumps({key: payload[key] for key in ("task_id", "worktree", "branch", "exit_code", "events", "final")}, ensure_ascii=False))
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--role", choices=sorted(ROLES), default="developer")
    parser.add_argument("--model", help="Override the model available to your Codex account")
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--worktrees", type=Path, default=Path("worktrees/codex"))
    parser.add_argument("--output", type=Path, default=Path("data/codex-tasks"))
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args()
    if not 60 <= args.timeout <= 86400:
        parser.error("timeout must be between 60 and 86400 seconds")
    try:
        return run(args)
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Codex task not started: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
