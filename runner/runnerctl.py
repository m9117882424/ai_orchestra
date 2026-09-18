#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
from pathlib import Path
from uuid import uuid4


def request(socket_path: Path, payload: dict) -> dict:
    encoded = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        response_timeout = max(10, int(payload.get("timeout_seconds", 0)) + 45)
        client.settimeout(response_timeout)
        client.connect(str(socket_path))
        client.sendall(encoded)
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            part = client.recv(65536)
            if not part:
                break
            chunks.extend(part)
            if len(chunks) > 4_194_304:
                raise RuntimeError("runnerd response too large")
    return json.loads(chunks)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default="/run/ai-orchestra/runnerd.sock")
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("health")
    run = sub.add_parser("run")
    run.add_argument("--repository-id", required=True)
    run.add_argument("--workspace-id", required=True)
    run.add_argument("--execution-id", required=True)
    run.add_argument("--base-commit", required=True)
    run.add_argument("--preflight-digest", required=True)
    run.add_argument("--source-snapshot-digest", default=None)
    run.add_argument("--request-id", default=None)
    run.add_argument("--timeout", type=int, default=60)
    run.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.operation == "health":
        payload = {"version": 1, "operation": "health"}
    else:
        argv = list(args.argv)
        if argv and argv[0] == "--":
            argv = argv[1:]
        if not argv:
            parser.error("run requires argv after --")
        payload = {
            "version": 1,
            "operation": "run",
            "request_id": args.request_id or str(uuid4()),
            "repository_id": args.repository_id,
            "workspace_id": args.workspace_id,
            "execution_id": args.execution_id,
            "base_commit": args.base_commit,
            "preflight_digest": args.preflight_digest,
            "argv": argv,
            "timeout_seconds": args.timeout,
        }
        if args.source_snapshot_digest:
            payload["source_snapshot_digest"] = args.source_snapshot_digest
    response = request(Path(args.socket), payload)
    print(json.dumps(response, ensure_ascii=False, sort_keys=True))
    return 0 if response.get("status") in {"ok", "completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
