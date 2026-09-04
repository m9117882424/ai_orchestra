#!/usr/bin/env python3
"""Smoke test for AI Orchestra's inference-only Model Gateway.

The script uses only MODEL_ROUTER_CLIENT_KEY. Provider and router-admin credentials
are never read or printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:18089/v1"

MODE_MODELS = {
    "shared": [
        "orchestra-lead",
        "orchestra-architect",
        "orchestra-coder",
        "orchestra-analyst",
        "orchestra-qa",
    ],
    "separate": [
        "orchestra-lead",      # Anthropic
        "orchestra-architect", # OpenAI
        "orchestra-analyst",   # Google
    ],
}


def request_json(base_url: str, api_key: str, method: str, path: str, payload: dict | None = None) -> dict:
    data = None
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base_url.rstrip("/") + "/" + path.lstrip("/"), data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {raw[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"unexpected response type: {type(parsed).__name__}")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("MODEL_GATEWAY_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--mode", choices=sorted(MODE_MODELS), default=os.getenv("KEY_MODE", "shared"))
    parser.add_argument("--all", action="store_true", help="test every logical alias, not only provider representatives")
    args = parser.parse_args()

    key = os.getenv("MODEL_ROUTER_CLIENT_KEY", "").strip()
    if not key:
        print("FAIL: MODEL_ROUTER_CLIENT_KEY is empty", file=sys.stderr)
        return 2

    catalog = request_json(args.base_url, key, "GET", "/models")
    ids = {str(row.get("id")) for row in catalog.get("data", []) if isinstance(row, dict)}
    required = set(MODE_MODELS[args.mode])
    missing = sorted(required - ids)
    if missing:
        print("FAIL: gateway catalog misses: " + ", ".join(missing), file=sys.stderr)
        return 1
    print(f"[OK] Model Gateway catalog: {len(ids)} aliases; required aliases present")

    models = sorted(ids) if args.all else MODE_MODELS[args.mode]
    failures = 0
    for model in models:
        try:
            result = request_json(
                args.base_url,
                key,
                "POST",
                "/chat/completions",
                {
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with exactly OK."}],
                    "max_tokens": 16,
                },
            )
            choices = result.get("choices") or []
            content = ""
            if choices:
                content = str((choices[0].get("message") or {}).get("content") or "")
            if "OK" not in content.upper():
                raise RuntimeError(f"unexpected response: {content[:80]!r}")
            print(f"[OK] {model}")
        except Exception as exc:
            failures += 1
            print(f"[FAIL] {model}: {exc}", file=sys.stderr)

    if failures:
        print(f"Model Gateway smoke: {failures} failure(s)", file=sys.stderr)
        return 1
    print(f"Model Gateway smoke: {len(models)}/{len(models)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
