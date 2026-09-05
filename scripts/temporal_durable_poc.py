#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker


@activity.defn
async def restart_sensitive_activity(marker_path: str) -> dict[str, Any]:
    info = activity.info()
    attempt = int(info.attempt)
    marker = Path(marker_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "attempt": attempt,
        "pid": os.getpid(),
        "activity_id": info.activity_id,
    }
    with marker.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    activity.heartbeat({"attempt": attempt, "pid": os.getpid()})
    if attempt == 1:
        # The parent process intentionally SIGKILLs this worker. Heartbeats make
        # the lost activity detectable so Temporal can retry it on worker #2.
        while True:
            await asyncio.sleep(0.25)
            activity.heartbeat({"attempt": attempt, "pid": os.getpid()})

    return {"attempt": attempt, "pid": os.getpid()}


@workflow.defn
class DurableWorkflow:
    @workflow.run
    async def run(self, marker_path: str) -> dict[str, Any]:
        result = await workflow.execute_activity(
            restart_sensitive_activity,
            marker_path,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=1),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=2),
                maximum_attempts=4,
            ),
        )
        return {
            "activity_attempt": int(result["attempt"]),
            "completed_by_pid": int(result["pid"]),
        }


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def connect_with_retry(address: str, timeout_seconds: float) -> Client:
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            return await Client.connect(address)
        except BaseException as exc:
            last_error = exc
            await asyncio.sleep(0.5)
    raise RuntimeError(f"Temporal did not become ready at {address}: {last_error}")


async def worker_main(address: str, task_queue: str, ready_file: Path) -> None:
    client = await connect_with_retry(address, 30)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[DurableWorkflow],
        activities=[restart_sensitive_activity],
    ):
        ready_file.write_text(str(os.getpid()), encoding="utf-8")
        await stop.wait()


async def wait_for_file(path: Path, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for {path}")


def read_marker_attempts(marker: Path) -> list[dict[str, Any]]:
    if not marker.exists():
        return []
    records: list[dict[str, Any]] = []
    for raw in marker.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            records.append(json.loads(raw))
    return records


async def terminate_process(proc: asyncio.subprocess.Process, graceful: bool) -> None:
    if proc.returncode is not None:
        return
    if graceful:
        proc.terminate()
    else:
        proc.kill()
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


async def start_worker_process(
    address: str,
    task_queue: str,
    ready_file: Path,
) -> asyncio.subprocess.Process:
    ready_file.unlink(missing_ok=True)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        "worker",
        "--address",
        address,
        "--task-queue",
        task_queue,
        "--ready-file",
        str(ready_file),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await wait_for_file(ready_file, 20)
    except BaseException:
        output = b""
        if proc.stdout is not None:
            try:
                output = await asyncio.wait_for(proc.stdout.read(), timeout=1)
            except asyncio.TimeoutError:
                pass
        await terminate_process(proc, graceful=False)
        raise RuntimeError(
            f"worker failed to become ready; output={output.decode('utf-8', errors='replace')[-4000:]}"
        )
    return proc


async def exercise(address: str, state_dir: Path, evidence_path: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / "activity-attempts.jsonl"
    marker.unlink(missing_ok=True)
    ready1 = state_dir / "worker-1.ready"
    ready2 = state_dir / "worker-2.ready"
    task_queue = f"ai-orchestra-durable-poc-{uuid.uuid4()}"
    workflow_id = f"ai-orchestra-durable-poc-{uuid.uuid4()}"

    client = await connect_with_retry(address, 30)
    worker1 = await start_worker_process(address, task_queue, ready1)
    worker2: asyncio.subprocess.Process | None = None
    started_at = time.time()
    try:
        handle = await client.start_workflow(
            DurableWorkflow.run,
            str(marker),
            id=workflow_id,
            task_queue=task_queue,
        )

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            attempts = read_marker_attempts(marker)
            if attempts and int(attempts[0].get("attempt", 0)) == 1:
                break
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError("first activity attempt did not start")

        worker1_pid = worker1.pid
        await terminate_process(worker1, graceful=False)

        worker2 = await start_worker_process(address, task_queue, ready2)
        result = await asyncio.wait_for(handle.result(), timeout=30)
        attempts = read_marker_attempts(marker)
        observed_attempts = [int(item["attempt"]) for item in attempts]
        if int(result.get("activity_attempt", 0)) < 2:
            raise RuntimeError(f"activity was not retried after worker loss: {result}")
        if 1 not in observed_attempts or max(observed_attempts) < 2:
            raise RuntimeError(f"marker does not prove retry across workers: {attempts}")

        evidence = {
            "workflow_id": workflow_id,
            "task_queue": task_queue,
            "worker_1_pid": worker1_pid,
            "worker_2_pid": worker2.pid,
            "worker_1_exit_code": worker1.returncode,
            "activity_attempts": attempts,
            "result": result,
            "result_sha256": canonical_sha256(result),
            "exercise_elapsed_seconds": round(time.time() - started_at, 3),
        }
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(evidence, sort_keys=True))
    finally:
        if worker1.returncode is None:
            await terminate_process(worker1, graceful=True)
        if worker2 is not None and worker2.returncode is None:
            await terminate_process(worker2, graceful=True)


async def verify(address: str, evidence_path: Path) -> None:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    client = await connect_with_retry(address, 30)
    handle = client.get_workflow_handle(str(evidence["workflow_id"]))
    result = await asyncio.wait_for(handle.result(), timeout=20)
    digest = canonical_sha256(result)
    expected = str(evidence["result_sha256"])
    if digest != expected:
        raise RuntimeError(f"persisted workflow result digest mismatch: {digest} != {expected}")
    if result != evidence["result"]:
        raise RuntimeError("persisted workflow result payload changed after server restart")
    print(
        json.dumps(
            {
                "workflow_id": evidence["workflow_id"],
                "result_sha256": digest,
                "server_restart_recovery": "verified",
            },
            sort_keys=True,
        )
    )


async def wait_server(address: str, timeout_seconds: float) -> None:
    await connect_with_retry(address, timeout_seconds)
    print(f"[OK] Temporal ready at {address}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Orchestra Temporal durability PoC")
    sub = parser.add_subparsers(dest="command", required=True)

    worker = sub.add_parser("worker")
    worker.add_argument("--address", required=True)
    worker.add_argument("--task-queue", required=True)
    worker.add_argument("--ready-file", type=Path, required=True)

    run = sub.add_parser("exercise")
    run.add_argument("--address", required=True)
    run.add_argument("--state-dir", type=Path, required=True)
    run.add_argument("--evidence", type=Path, required=True)

    check = sub.add_parser("verify")
    check.add_argument("--address", required=True)
    check.add_argument("--evidence", type=Path, required=True)

    wait = sub.add_parser("wait")
    wait.add_argument("--address", required=True)
    wait.add_argument("--timeout", type=float, default=90)

    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    if args.command == "worker":
        await worker_main(args.address, args.task_queue, args.ready_file)
    elif args.command == "exercise":
        await exercise(args.address, args.state_dir, args.evidence)
    elif args.command == "verify":
        await verify(args.address, args.evidence)
    elif args.command == "wait":
        await wait_server(args.address, args.timeout)
    else:
        raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    asyncio.run(async_main())
