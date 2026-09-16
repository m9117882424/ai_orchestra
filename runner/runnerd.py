#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import socketserver
import stat
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

LOG = logging.getLogger("ai_orchestra.runnerd")
PROTOCOL_VERSION = 1
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
REQUEST_KEYS = frozenset({
    "version", "operation", "request_id", "workspace_id", "execution_id",
    "base_commit", "preflight_digest", "source_snapshot_digest", "argv", "timeout_seconds",
})


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a UUID string")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be canonical UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"{field} must be canonical UUID")
    return value


@dataclass(frozen=True)
class RunnerConfig:
    socket_path: Path
    workspace_volume: str
    image_id: str
    docker_bin: str = "/usr/bin/docker"
    max_timeout_seconds: int = 900
    max_output_bytes: int = 1_048_576
    max_concurrent: int = 2
    memory: str = "1g"
    cpus: str = "1.0"
    pids_limit: int = 128
    workspace_tmpfs_bytes: int = 536_870_912

    @classmethod
    def from_env(cls) -> "RunnerConfig":
        socket_path = Path(os.getenv("RUNNERD_SOCKET_PATH", "/run/ai-orchestra/runnerd.sock"))
        volume = os.getenv("RUNNERD_WORKSPACE_VOLUME", "ai-development-department_task-workspaces")
        image_id = os.getenv("RUNNERD_IMAGE_ID", "")
        if not socket_path.is_absolute() or socket_path.name != "runnerd.sock":
            raise ValueError("RUNNERD_SOCKET_PATH must be an absolute runnerd.sock path")
        if not VOLUME_RE.fullmatch(volume):
            raise ValueError("RUNNERD_WORKSPACE_VOLUME is invalid")
        if not IMAGE_ID_RE.fullmatch(image_id):
            raise ValueError("RUNNERD_IMAGE_ID must be an immutable sha256 image id")
        max_timeout = int(os.getenv("RUNNERD_MAX_TIMEOUT_SECONDS", "900"))
        max_output = int(os.getenv("RUNNERD_MAX_OUTPUT_BYTES", "1048576"))
        max_concurrent = int(os.getenv("RUNNERD_MAX_CONCURRENT", "2"))
        pids_limit = int(os.getenv("RUNNERD_PIDS_LIMIT", "128"))
        workspace_tmpfs = int(os.getenv("RUNNERD_WORKSPACE_TMPFS_BYTES", "536870912"))
        if not 10 <= max_timeout <= 3600:
            raise ValueError("RUNNERD_MAX_TIMEOUT_SECONDS must be between 10 and 3600")
        if not 4096 <= max_output <= 16_777_216:
            raise ValueError("RUNNERD_MAX_OUTPUT_BYTES must be between 4096 and 16777216")
        if not 1 <= max_concurrent <= 8:
            raise ValueError("RUNNERD_MAX_CONCURRENT must be between 1 and 8")
        if not 16 <= pids_limit <= 1024:
            raise ValueError("RUNNERD_PIDS_LIMIT must be between 16 and 1024")
        if not 67_108_864 <= workspace_tmpfs <= 4_294_967_296:
            raise ValueError("RUNNERD_WORKSPACE_TMPFS_BYTES must be between 64MiB and 4GiB")
        return cls(
            socket_path=socket_path,
            workspace_volume=volume,
            image_id=image_id,
            docker_bin=os.getenv("RUNNERD_DOCKER_BIN", "/usr/bin/docker"),
            max_timeout_seconds=max_timeout,
            max_output_bytes=max_output,
            max_concurrent=max_concurrent,
            memory=os.getenv("RUNNERD_MEMORY", "1g"),
            cpus=os.getenv("RUNNERD_CPUS", "1.0"),
            pids_limit=pids_limit,
            workspace_tmpfs_bytes=workspace_tmpfs,
        )


@dataclass(frozen=True)
class RunRequest:
    request_id: str
    workspace_id: str
    execution_id: str
    base_commit: str
    preflight_digest: str
    source_snapshot_digest: str | None
    argv: tuple[str, ...]
    timeout_seconds: int


def parse_run_request(payload: object, config: RunnerConfig) -> RunRequest:
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    unknown = set(payload) - REQUEST_KEYS
    if unknown:
        raise ValueError("unsupported request fields: " + ",".join(sorted(unknown)))
    if payload.get("version") != PROTOCOL_VERSION or payload.get("operation") != "run":
        raise ValueError("unsupported protocol version or operation")
    request_id = canonical_uuid(payload.get("request_id"), "request_id")
    workspace_id = canonical_uuid(payload.get("workspace_id"), "workspace_id")
    execution_id = canonical_uuid(payload.get("execution_id"), "execution_id")
    base_commit = payload.get("base_commit")
    preflight_digest = payload.get("preflight_digest")
    source_snapshot_digest = payload.get("source_snapshot_digest")
    if not isinstance(base_commit, str) or not COMMIT_RE.fullmatch(base_commit):
        raise ValueError("base_commit must be an immutable hex commit")
    if not isinstance(preflight_digest, str) or not DIGEST_RE.fullmatch(preflight_digest):
        raise ValueError("preflight_digest must be a sha256 hex digest")
    if source_snapshot_digest is not None and (
        not isinstance(source_snapshot_digest, str)
        or not DIGEST_RE.fullmatch(source_snapshot_digest)
    ):
        raise ValueError("source_snapshot_digest must be a sha256 hex digest")
    argv = payload.get("argv")
    if not isinstance(argv, list) or not 1 <= len(argv) <= 64:
        raise ValueError("argv must contain between 1 and 64 strings")
    normalized: list[str] = []
    total = 0
    for item in argv:
        if not isinstance(item, str) or not item or "\x00" in item or len(item) > 4096:
            raise ValueError("argv contains an invalid item")
        total += len(item)
        normalized.append(item)
    if total > 32768:
        raise ValueError("argv is too large")
    timeout = payload.get("timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise ValueError("timeout_seconds must be an integer")
    if not 1 <= timeout <= config.max_timeout_seconds:
        raise ValueError("timeout_seconds is outside the allowed range")
    return RunRequest(
        request_id, workspace_id, execution_id, base_commit, preflight_digest,
        source_snapshot_digest, tuple(normalized), timeout,
    )


def container_name(request_id: str) -> str:
    return "ai-orchestra-runner-" + request_id.replace("-", "")

def build_docker_command(config: RunnerConfig, request: RunRequest) -> list[str]:
    source_mount = (
        f"type=volume,src={config.workspace_volume},dst=/source,"
        f"volume-subpath={request.workspace_id},readonly"
    )
    return [
        config.docker_bin, "run", "--rm", "--pull", "never", "--init",
        "--name", container_name(request.request_id),
        "--label", f"ai-orchestra.runner.request={request.request_id}",
        "--label", f"ai-orchestra.runner.workspace={request.workspace_id}",
        "--network", "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", str(config.pids_limit),
        "--memory", config.memory,
        "--cpus", config.cpus,
        "--user", "10001:10001",
        "--workdir", "/workspace",
        "--ulimit", "core=0:0",
        "--ulimit", "nofile=1024:1024",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=268435456,mode=1777",
        "--tmpfs", "/home/orchestra:rw,nosuid,nodev,noexec,size=67108864,uid=10001,gid=10001,mode=0700",
        "--tmpfs", f"/workspace:rw,nosuid,nodev,size={config.workspace_tmpfs_bytes},uid=10001,gid=10001,mode=0700",
        "--mount", source_mount,
        "--env", "HOME=/home/orchestra",
        "--env", "CI=1",
        "--env", "NO_COLOR=1",
        "--env", f"AI_ORCHESTRA_RUNNER_WORKSPACE_ID={request.workspace_id}",
        "--env", f"AI_ORCHESTRA_RUNNER_EXECUTION_ID={request.execution_id}",
        "--env", f"AI_ORCHESTRA_RUNNER_BASE_COMMIT={request.base_commit}",
        "--env", f"AI_ORCHESTRA_RUNNER_PREFLIGHT_DIGEST={request.preflight_digest}",
        *(
            ["--env", f"AI_ORCHESTRA_RUNNER_SOURCE_SNAPSHOT_DIGEST={request.source_snapshot_digest}"]
            if request.source_snapshot_digest is not None
            else []
        ),
        config.image_id,
        *request.argv,
    ]


class BoundedCapture:
    def __init__(self, limit: int):
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def drain(self, stream: BinaryIO) -> None:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            remaining = self.limit - len(self.data)
            if remaining > 0:
                self.data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.truncated = True

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")

class DockerRunner:
    def __init__(self, config: RunnerConfig):
        self.config = config

    def preflight(self) -> None:
        image = subprocess.run(
            [self.config.docker_bin, "image", "inspect", self.config.image_id],
            check=True, capture_output=True, text=True, timeout=15,
        )
        payload = json.loads(image.stdout)
        if not isinstance(payload, list) or len(payload) != 1:
            raise RuntimeError("runner image inspect returned unexpected data")
        item = payload[0]
        if item.get("Id") != self.config.image_id:
            raise RuntimeError("runner image id mismatch")
        config = item.get("Config") or {}
        if config.get("Entrypoint") != ["python3", "/opt/ai-orchestra-runner/entrypoint.py"]:
            raise RuntimeError("runner image entrypoint mismatch")
        subprocess.run(
            [self.config.docker_bin, "volume", "inspect", self.config.workspace_volume],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )

    def cleanup(self, request_id: str) -> bool:
        name = container_name(request_id)
        subprocess.run(
            [self.config.docker_bin, "rm", "-f", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False,
        )
        check = subprocess.run(
            [self.config.docker_bin, "container", "inspect", name],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if check.returncode == 0:
            return False
        message = (check.stderr or check.stdout).lower()
        return "no such" in message
    def run(self, request: RunRequest) -> dict:
        started_at = utc_iso()
        stdout_capture = BoundedCapture(self.config.max_output_bytes)
        stderr_capture = BoundedCapture(self.config.max_output_bytes)
        process = subprocess.Popen(
            build_docker_command(self.config, request),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        stdout_thread = threading.Thread(
            target=stdout_capture.drain, args=(process.stdout,), daemon=True
        )
        stderr_thread = threading.Thread(
            target=stderr_capture.drain, args=(process.stderr,), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        cleanup_confirmed = True
        try:
            exit_code = process.wait(timeout=request.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            cleanup_confirmed = self.cleanup(request.request_id)
            try:
                exit_code = process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = process.wait(timeout=5)
            cleanup_confirmed = self.cleanup(request.request_id) and cleanup_confirmed
        finally:
            stdout_thread.join(timeout=10)
            stderr_thread.join(timeout=10)
        if not timed_out:
            cleanup_confirmed = self.cleanup(request.request_id)
        if not cleanup_confirmed:
            status = "cleanup_uncertain"
        elif timed_out:
            status = "timed_out"
        else:
            status = "completed" if exit_code == 0 else "failed"
        return {
            "version": PROTOCOL_VERSION,
            "request_id": request.request_id,
            "workspace_id": request.workspace_id,
            "execution_id": request.execution_id,
            "status": status,
            "exit_code": None if timed_out else exit_code,
            "stdout": stdout_capture.text(),
            "stderr": stderr_capture.text(),
            "output_truncated": stdout_capture.truncated or stderr_capture.truncated,
            "runner_image_id": self.config.image_id,
            "cleanup_confirmed": cleanup_confirmed,
            "started_at": started_at,
            "finished_at": utc_iso(),
        }


class RunnerServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, config: RunnerConfig, runner: DockerRunner):
        self.config = config
        self.runner = runner
        self.slots = threading.BoundedSemaphore(config.max_concurrent)
        super().__init__(str(config.socket_path), RunnerHandler)

class RunnerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: RunnerServer = self.server  # type: ignore[assignment]
        raw = self.rfile.readline(65537)
        if len(raw) > 65536 or not raw.endswith(b"\n"):
            self._reply({
                "version": PROTOCOL_VERSION,
                "status": "rejected",
                "error": "request_too_large",
            })
            return
        try:
            payload = json.loads(raw)
            if payload == {"version": PROTOCOL_VERSION, "operation": "health"}:
                self._reply({
                    "version": PROTOCOL_VERSION,
                    "status": "ok",
                    "runner_image_id": server.config.image_id,
                })
                return
            request = parse_run_request(payload, server.config)
        except (ValueError, json.JSONDecodeError) as exc:
            self._reply({
                "version": PROTOCOL_VERSION,
                "status": "rejected",
                "error": str(exc)[:500],
            })
            return
        if not server.slots.acquire(blocking=False):
            self._reply({
                "version": PROTOCOL_VERSION,
                "request_id": request.request_id,
                "status": "busy",
            })
            return
        try:
            LOG.info(
                "runner start request_id=%s workspace_id=%s",
                request.request_id,
                request.workspace_id,
            )
            response = server.runner.run(request)
            LOG.info(
                "runner finish request_id=%s status=%s",
                request.request_id,
                response["status"],
            )
            self._reply(response)
        except Exception as exc:
            LOG.exception("runner failure request_id=%s", request.request_id)
            server.runner.cleanup(request.request_id)
            self._reply({
                "version": PROTOCOL_VERSION,
                "request_id": request.request_id,
                "workspace_id": request.workspace_id,
                "status": "error",
                "error": type(exc).__name__,
            })
        finally:
            server.slots.release()

    def _reply(self, payload: dict) -> None:
        self.wfile.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            + b"\n"
        )


def prepare_socket_path(path: Path) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if parent.is_symlink():
        raise RuntimeError("runnerd socket directory must not be a symlink")
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise RuntimeError("refusing to replace unexpected runnerd socket path")
        path.unlink()


def serve(config: RunnerConfig) -> int:
    if os.geteuid() != 0 and os.getenv("RUNNERD_ALLOW_NONROOT") != "1":
        raise RuntimeError(
            "runnerd must run as root or set RUNNERD_ALLOW_NONROOT=1 for test only"
        )
    runner = DockerRunner(config)
    runner.preflight()
    prepare_socket_path(config.socket_path)
    with RunnerServer(config, runner) as server:
        os.chmod(config.socket_path, 0o660)
        LOG.info(
            "runnerd ready socket=%s image=%s volume=%s",
            config.socket_path,
            config.image_id,
            config.workspace_volume,
        )

        def shutdown(_signum, _frame):
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        server.serve_forever(poll_interval=0.5)
    try:
        config.socket_path.unlink(missing_ok=True)
    except OSError:
        pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "check"])
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = RunnerConfig.from_env()
    runner = DockerRunner(config)
    if args.command == "check":
        runner.preflight()
        print("[OK] runnerd preflight")
        return 0
    return serve(config)


if __name__ == "__main__":
    raise SystemExit(main())
