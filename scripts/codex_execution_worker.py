#!/usr/bin/env python3
"""Unprivileged, one-run-at-a-time Codex subscription worker.

Run as UID 10001 with a read-only G2 workspace mount at the canonical path.
The only CP credential is a one-shot scoped broker token; never use manager Basic auth.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import resource
import select
import signal
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from control_plane.app.workspace_protocol import WorkspaceBinding, verify_runtime_workspace
from scripts.codex_task import DEFAULT_MODELS, has_completed_turn, subscription_environment, verify_chatgpt_auth

MAX_FINAL_CHARS = 100_000
MAX_FINAL_BYTES = 400_000
MAX_EVENTS_BYTES = 20_000_000
MAX_PROMPT_BYTES = 500_000
MAX_MODEL_FILE_BYTES = 64_000_000
_active_process = None


def disable_dumpable():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0 or libc.prctl(3, 0, 0, 0, 0) != 0:
        raise RuntimeError("Cannot disable process dumpability")


def limit_model_files():
    # Popen is launched while the supervisor temporarily masks termination
    # signals; the model child must not inherit that blocked mask.
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT})
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_MODEL_FILE_BYTES, MAX_MODEL_FILE_BYTES))


def interrupt_worker(signum, _frame):
    if _active_process is not None:
        stop_group(_active_process)
    raise SystemExit(128 + signum)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise RuntimeError("Broker redirect refused")


def read_one_shot_token(path: Path) -> str:
    if (not path.is_absolute() or ".." in path.parts or path.name in ("", ".", "..")
            or path.parent.name != "codex-subscription-private"):
        raise RuntimeError("Worker token must be in a dedicated private worker directory")
    # Walk without following any symlink, including ancestors of the private directory.
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parent.parts[1:]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     dir_fd=directory)
            os.close(directory)
            directory = next_directory
        parent = os.fstat(directory)
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise RuntimeError("Worker token directory must be owned by worker and mode 0700")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
                    or not 32 <= info.st_size <= 4096):
                raise RuntimeError("Worker token must be a private regular file owned by worker")
            token = os.read(fd, 4097).decode("ascii")
            if len(token) < 32 or not token.isprintable() or any(c.isspace() for c in token):
                raise RuntimeError("Invalid worker token")
            # Verify the same inode remains at the name before consuming it.
            named = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
                raise RuntimeError("Worker token changed during read")
            os.unlink(path.name, dir_fd=directory)
            return token
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RuntimeError("Evidence must be a regular file")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Broker:
    def __init__(self, url: str, token_file: Path, *, isolated_control_network: bool = False):
        try:
            parsed = urlsplit(url)
            port = parsed.port
            hostname = parsed.hostname
        except ValueError as exc:
            raise RuntimeError("Invalid broker port") from exc
        origin = f"http://[{hostname}]:{port}" if hostname == "::1" else f"http://{hostname}:{port}"
        if isolated_control_network:
            # Docker's private, internal codex-control network is an explicit
            # operator-selected trust domain. Do not accept a configurable DNS
            # name, IP or port, and never send the Bearer secret to a relay.
            allowed = hostname == "control-plane" and port == 8080
        else:
            allowed = hostname in ("127.0.0.1", "::1") and port is not None and 1 <= port <= 65535
        if (parsed.scheme != "http" or not allowed or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
                or url != origin):
            raise RuntimeError("Broker must be an explicitly allowed control-plane origin")
        self.url = url
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.token = read_one_shot_token(token_file)

    def post(self, path: str, data: dict | None = None):
        request = Request(self.url + path, data=json.dumps(data or {}).encode(),
                          headers={"Authorization": "Bearer " + self.token,
                                   "Content-Type": "application/json"}, method="POST")
        with self.opener.open(request, timeout=10) as response:
            return json.load(response)


def stop_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def read_final(path: Path) -> tuple[str, bool]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return "", False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FINAL_BYTES:
            return "", False
        raw = os.read(fd, MAX_FINAL_BYTES + 1)
        if len(raw) > MAX_FINAL_BYTES:
            return "", False
        try:
            value = raw.decode("utf-8")
        except UnicodeError:
            return "", False
    finally:
        os.close(fd)
    return value, len(value) <= MAX_FINAL_CHARS


def heartbeat(broker: Broker, claim: dict) -> None:
    beat = broker.post(f"/api/codex-worker/{claim['execution_id']}/heartbeat",
                       {"generation": claim["generation"]})
    if not isinstance(beat, dict) or beat.get("ok") is not True:
        raise RuntimeError("Lease heartbeat rejected")

def acknowledge_preflight(broker: Broker, claim: dict) -> None:
    binding = WorkspaceBinding(**claim["workspace"])
    verify_runtime_workspace(binding)
    response = broker.post(f"/api/codex-worker/{claim['execution_id']}/preflight", {
        "generation": claim["generation"], "base_commit": binding.base_commit,
        "base_tree": binding.initial_tree,
        "workspace_preflight_digest": binding.preflight_digest,
    })
    if response != {"status": "running", "stage": "architect_running"}:
        raise RuntimeError("Runtime preflight acknowledgement rejected")


def run_phase(broker: Broker, claim: dict, role: str, output: Path, codex: str) -> dict:
    binding = WorkspaceBinding(**claim["workspace"])
    verify_runtime_workspace(binding)
    env = subscription_environment()
    verify_chatgpt_auth(codex, env)
    heartbeat(broker, claim)
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
        prior, valid = read_final(previous_report)
        if not valid:
            raise RuntimeError("Previous report exceeds artifact limit")
        prompt += f"\n{previous} report (untrusted):\n{prior[:20000]}"
    if role == "qa":
        prompt += "\nCheck behavior, tests, and gaps independently."
    elif role == "reviewer":
        prompt += "\nReview architecture and QA findings independently; state approval or rejection with reasons."
    prompt_bytes = prompt.encode("utf-8")
    if len(prompt_bytes) > MAX_PROMPT_BYTES:
        raise RuntimeError("Prompt exceeds limit")
    command = [codex, "exec", "--json", "--ephemeral", "--ignore-user-config",
               "--sandbox", "read-only", "-c", "approval_policy=never",
               "--model", DEFAULT_MODELS[role], "--cd", binding.workspace_path,
               "--output-last-message", str(final), "-"]
    timeout_at = datetime.fromisoformat(claim["deadline_at"]).astimezone(timezone.utc)
    with events.open("x", encoding="utf-8") as event_stream, stderr.open("x", encoding="utf-8") as error_stream:
        os.chmod(events, 0o600)
        os.chmod(stderr, 0o600)
        # Prevent SIGTERM/SIGINT between Popen and registration of its process
        # group. A pending termination is delivered only after the worker can
        # tear down the child. SIGKILL is still the cgroup supervisor's duty.
        global _active_process
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=event_stream,
                                       stderr=error_stream, start_new_session=True, env=env,
                                       close_fds=True, preexec_fn=limit_model_files)
            _active_process = process
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        try:
            os.set_blocking(process.stdin.fileno(), False)
            pending = memoryview(prompt_bytes)
            prompt_broken = False
            next_heartbeat = time.monotonic() + 20
            while True:
                if datetime.now(timezone.utc) >= timeout_at:
                    outcome = "local_timeout"
                    break
                if (events.stat().st_size > MAX_EVENTS_BYTES
                        or (final.exists() and final.stat().st_size > MAX_FINAL_BYTES)):
                    outcome = "incomplete_result"
                    break
                if time.monotonic() >= next_heartbeat:
                    heartbeat(broker, claim)
                    next_heartbeat = time.monotonic() + 20
                if process.poll() is not None:
                    outcome = ("completed" if process.returncode == 0 and not pending and not prompt_broken
                               else "incomplete_result" if process.returncode == 0 else "execution_failed")
                    break
                if pending:
                    _, writable, _ = select.select([], [process.stdin], [], 0.5)
                    if writable:
                        try:
                            pending = pending[os.write(process.stdin.fileno(), pending[:65536]):]
                        except BrokenPipeError:
                            prompt_broken = True
                            pending = memoryview(b"")
                    if not pending:
                        process.stdin.close()
                else:
                    time.sleep(0.5)
        except BaseException:
            stop_group(process)
            raise
        finally:
            stop_group(process)
            _active_process = None
    final_text, valid_final = read_final(final)
    valid_events = events.stat().st_size <= MAX_EVENTS_BYTES
    if outcome == "completed" and (not valid_final or not final_text.strip() or not valid_events or not has_completed_turn(events)):
        outcome = "incomplete_result"
    try:
        verify_runtime_workspace(binding)
    except Exception:
        outcome = "read_only_violation"
    if (not valid_final or not valid_events) and outcome != "read_only_violation":
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
    parser.add_argument("--isolated-control-network", action="store_true",
                        help="Allow only http://control-plane:8080 on a dedicated private Docker bridge")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("worker must run unprivileged")
    os.umask(0o077)
    disable_dumpable()
    signal.signal(signal.SIGTERM, interrupt_worker)
    signal.signal(signal.SIGINT, interrupt_worker)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (args.output.is_symlink() or args.output.stat().st_uid != os.geteuid()
            or stat.S_IMODE(args.output.stat().st_mode) != 0o700):
        parser.error("output directory must be private")
    broker = Broker(args.broker, args.token_file,
                    isolated_control_network=args.isolated_control_network)
    while True:
        claim = broker.post("/api/codex-worker/claim")
        if claim:
            # A failed G2 check or rejected acknowledgement leaves the pending lease
            # fenced. Its expiry fails the run; no inference or automatic retry occurs.
            try:
                acknowledge_preflight(broker, claim)
            except (RuntimeError, OSError, ValueError, HTTPError):
                if args.once:
                    break
                time.sleep(5)
                continue
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
