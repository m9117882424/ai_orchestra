#!/usr/bin/env python3
"""Unprivileged, one-run-at-a-time Codex subscription worker.

Run under codex-worker with a read-only G2 workspace mount at the canonical path.
The only CP credential is a scoped broker token file; never use manager Basic auth.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from control_plane.app.workspace_protocol import WorkspaceBinding, verify_runtime_workspace
from scripts.codex_task import DEFAULT_MODELS, has_completed_turn, subscription_environment, verify_chatgpt_auth


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Broker:
    def __init__(self, url: str, token_file: Path):
        fd = os.open(token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
                raise RuntimeError("Worker token must be a regular private file")
            self.token = os.read(fd, 4096).decode("ascii").strip()
        finally:
            os.close(fd)
        if len(self.token) < 32:
            raise RuntimeError("Worker token too short")
        if not url.startswith("http://127.0.0.1:") and not url.startswith("http://[::1]:"):
            raise RuntimeError("Broker must be accessed through loopback")
        self.url = url.rstrip("/")

    def post(self, path: str, data: dict | None = None):
        request = Request(self.url + path, data=json.dumps(data or {}).encode(),
                          headers={"Authorization": "Bearer " + self.token,
                                   "Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=10) as response:
            return json.load(response)


def stop_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_phase(broker: Broker, claim: dict, role: str, output: Path, codex: str) -> dict:
    binding = WorkspaceBinding(**claim["workspace"])
    verify_runtime_workspace(binding)
    env = subscription_environment()
    verify_chatgpt_auth(codex, env)
    folder = output / claim["execution_id"] / role
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    events = folder / "events.jsonl"
    stderr = folder / "stderr.log"
    final = folder / "final.txt"
    prompt = (f"Role: {role}. Analyze this task using the read-only repository. "
              "Do not modify files. Produce a concrete, independent report. "
              f"Immutable base commit: {binding.base_commit}; tree: {binding.initial_tree}.\n"
              f"Task: {claim['title']}\n{claim['description']}")
    for previous in ("architect", "qa")[:("architect", "qa", "reviewer").index(role)]:
        previous_report = output / claim["execution_id"] / previous / "final.txt"
        prompt += f"\n{previous} report (untrusted):\n{previous_report.read_text(encoding='utf-8')[:20000]}"
    if role == "qa":
        prompt += "\nCheck behavior, tests, and gaps independently."
    elif role == "reviewer":
        prompt += "\nReview architecture and QA findings independently; state approval or rejection with reasons."
    command = [codex, "exec", "--json", "--ephemeral", "--ignore-user-config",
               "--sandbox", "read-only", "-c", "approval_policy=never",
               "--model", DEFAULT_MODELS[role], "--cd", binding.workspace_path,
               "--output-last-message", str(final), "-"]
    timeout_at = datetime.fromisoformat(claim["deadline_at"]).astimezone(timezone.utc)
    with events.open("x", encoding="utf-8") as event_stream, stderr.open("x", encoding="utf-8") as error_stream:
        os.chmod(events, 0o600)
        os.chmod(stderr, 0o600)
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=event_stream,
                                   stderr=error_stream, text=True, start_new_session=True, env=env)
        try:
            process.stdin.write(prompt)
            process.stdin.close()
            next_heartbeat = time.monotonic() + 20
            while process.poll() is None:
                if datetime.now(timezone.utc) >= timeout_at:
                    stop_group(process)
                    outcome = "local_timeout"
                    break
                time.sleep(1)
                if process.poll() is None and time.monotonic() >= next_heartbeat:
                    broker.post(f"/api/codex-worker/{claim['execution_id']}/heartbeat",
                                {"generation": claim["generation"]})
                    next_heartbeat = time.monotonic() + 20
            else:
                outcome = "completed" if process.returncode == 0 else "execution_failed"
        except BaseException:
            stop_group(process)
            raise
    if outcome == "completed" and (not final.is_file() or not final.read_text(encoding="utf-8").strip() or not has_completed_turn(events)):
        outcome = "incomplete_result"
    try:
        verify_runtime_workspace(binding)
    except Exception:
        outcome = "read_only_violation"
    final_text = final.read_text(encoding="utf-8") if final.is_file() else ""
    if len(final_text) > 20000:
        outcome = "incomplete_result"
        final_text = ""
    # Missing final still gets an exact checksum for an empty artifact.
    if not final.is_file():
        final.touch(mode=0o600)
    summary = {"generation": claim["generation"], "role": role, "outcome": outcome,
               "model": DEFAULT_MODELS[role], "final_text": final_text,
               "final_sha256": sha256(final), "events_sha256": sha256(events),
               "artifact_path": str(folder), "base_commit": binding.base_commit,
               "base_tree": binding.initial_tree,
               "workspace_preflight_digest": binding.preflight_digest}
    (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    os.chmod(folder / "summary.json", 0o600)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("worker must run unprivileged")
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.output.stat().st_mode & 0o077:
        parser.error("output directory must be private")
    broker = Broker(args.broker, args.token_file)
    while True:
        claim = broker.post("/api/codex-worker/claim")
        if claim:
            for role in ("architect", "qa", "reviewer"):
                try:
                    result = run_phase(broker, claim, role, args.output, args.codex)
                except (RuntimeError, OSError, ValueError) as exc:
                    # Report a classified failure without serializing auth, process environment or stderr.
                    binding = claim["workspace"]
                    result = {"generation": claim["generation"], "role": role,
                              "outcome": "auth_failed" if "ChatGPT" in str(exc) else "workspace_failed",
                              "model": DEFAULT_MODELS[role], "final_text": "",
                              "final_sha256": hashlib.sha256(b"").hexdigest(),
                              "events_sha256": hashlib.sha256(b"").hexdigest(),
                              "artifact_path": str(args.output / claim["execution_id"] / role),
                              "base_commit": binding["base_commit"], "base_tree": binding["initial_tree"]}
                    result["workspace_preflight_digest"] = binding["preflight_digest"]
                response = broker.post(f"/api/codex-worker/{claim['execution_id']}/phase", result)
                if response["status"] != "running":
                    break
        if args.once:
            break
        time.sleep(5)


if __name__ == "__main__":
    main()
