#!/usr/bin/env python3
"""Low-cost smoke test for the AITUNNEL API.

The script uses only the Python standard library and never prints the API key.
Set AITUNNEL_API_KEY in the environment before a real run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_BASE_URL = "https://api.aitunnel.ru/v1"
DEFAULT_MODEL = "gpt-4o-mini"


class ApiError(RuntimeError):
    """AITUNNEL HTTP or protocol error."""


@dataclass
class TestResult:
    name: str
    status: str
    detail: str
    elapsed_s: float = 0.0


class AITunnelClient:
    def __init__(self, api_key: str, base_url: str, timeout: float) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> urllib.request.Request:
        data = None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "aitunnel-smoke-test/1.0",
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return urllib.request.Request(
            f"{self.base_url}/{path.lstrip('/')}",
            data=data,
            headers=headers,
            method=method,
        )

    def json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = self._request(method, path, payload)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise ApiError(f"HTTP {exc.code}: {safe_error(raw)}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"Network error: {exc.reason}") from exc

        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiError(f"Invalid JSON response: {raw[:300]}") from exc
        if not isinstance(result, dict):
            raise ApiError(f"Unexpected response type: {type(result).__name__}")
        return result

    def stream_chat(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        request = self._request("POST", "/chat/completions", payload)
        chunks: list[str] = []
        usage: dict[str, Any] = {}
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    event = line[5:].strip()
                    if event == "[DONE]":
                        break
                    try:
                        data = json.loads(event)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data.get("usage"), dict):
                        usage = data["usage"]
                    for choice in data.get("choices") or []:
                        content = (choice.get("delta") or {}).get("content")
                        if isinstance(content, str):
                            chunks.append(content)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise ApiError(f"HTTP {exc.code}: {safe_error(raw)}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"Network error: {exc.reason}") from exc
        return "".join(chunks), usage


def safe_error(raw: str) -> str:
    """Return a short server error without accidentally dumping large content."""
    try:
        parsed = json.loads(raw)
        message = (parsed.get("error") or {}).get("message")
        if message:
            return str(message)[:500]
    except (json.JSONDecodeError, AttributeError):
        pass
    return raw.replace("\n", " ")[:500]


def usage_detail(data: dict[str, Any]) -> str:
    usage = data.get("usage") or {}
    parts: list[str] = []
    if "total_tokens" in usage:
        parts.append(f"tokens={usage['total_tokens']}")
    if "cost_rub" in usage:
        parts.append(f"cost={usage['cost_rub']} RUB")
    if "balance" in usage:
        parts.append(f"balance={usage['balance']} RUB")
    return ", ".join(parts) or "usage metadata not returned"


def message_from_completion(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ApiError("Response contains no completion choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ApiError("Response contains no assistant message")
    return message


def run_case(name: str, function: Callable[[], str]) -> TestResult:
    started = time.monotonic()
    try:
        detail = function()
        return TestResult(name, "PASS", detail, time.monotonic() - started)
    except Exception as exc:  # A smoke test should report all failures in one run.
        return TestResult(name, "FAIL", str(exc), time.monotonic() - started)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Low-cost AITUNNEL API compatibility test",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--base-url", default=os.getenv("AITUNNEL_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.getenv("AITUNNEL_MODEL", DEFAULT_MODEL))
    parser.add_argument("--max-tokens", type=int, default=160)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument(
        "--full",
        action="store_true",
        help="also test JSON schema, forced tool call, streaming, and Responses API",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show planned checks without making billable requests",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    planned = ["balance", "models", "chat.completions"]
    if args.full:
        planned += ["structured output", "tool calling", "streaming", "responses", "stats"]

    print("AITUNNEL smoke test")
    print(f"Base URL: {args.base_url}")
    print(f"Model: {args.model}")
    print("Checks: " + ", ".join(planned))

    if args.dry_run:
        print("DRY RUN: no network requests were sent and no balance was spent.")
        return 0

    api_key = os.getenv("AITUNNEL_API_KEY", "").strip()
    if not api_key:
        print(
            "ERROR: set AITUNNEL_API_KEY in the environment. "
            "Do not put the key into this file or commit it to Git.",
            file=sys.stderr,
        )
        return 2

    client = AITunnelClient(api_key, args.base_url, args.timeout)
    results: list[TestResult] = []

    def test_balance() -> str:
        data = client.json_request("GET", "/aitunnel/balance")
        balance = data.get("balance")
        budget = data.get("budget", "not set")
        return f"balance={balance} RUB, key budget={budget}"

    def test_models() -> str:
        data = client.json_request("GET", "/models")
        models = data.get("data") or []
        ids = {item.get("id") for item in models if isinstance(item, dict)}
        if args.model not in ids:
            sample = ", ".join(sorted(str(item) for item in ids if item)[:8])
            raise ApiError(f"model {args.model!r} not found; sample: {sample}")
        return f"available models={len(ids)}, selected model found"

    def test_chat() -> str:
        data = client.json_request(
            "POST",
            "/chat/completions",
            {
                "model": args.model,
                "messages": [
                    {"role": "system", "content": "Reply with exactly AITUNNEL_OK."},
                    {"role": "user", "content": "Connectivity check."},
                ],
                "temperature": 0,
                "max_tokens": args.max_tokens,
            },
        )
        content = message_from_completion(data).get("content") or ""
        if "AITUNNEL_OK" not in content:
            raise ApiError(f"unexpected model answer: {content[:160]!r}")
        return usage_detail(data)

    results.append(run_case("Balance and authentication", test_balance))
    results.append(run_case("Model catalog", test_models))
    results.append(run_case("Chat completion", test_chat))

    if args.full:
        def test_structured_output() -> str:
            data = client.json_request(
                "POST",
                "/chat/completions",
                {
                    "model": args.model,
                    "messages": [{
                        "role": "user",
                        "content": "Return status for test vehicle TEST001: online and zero violations.",
                    }],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "vehicle_status",
                            "strict": True,
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "plate": {"type": "string"},
                                    "online": {"type": "boolean"},
                                    "violations": {"type": "integer"},
                                },
                                "required": ["plate", "online", "violations"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "max_tokens": args.max_tokens,
                },
            )
            content = message_from_completion(data).get("content") or ""
            parsed = json.loads(content)
            if set(parsed) != {"plate", "online", "violations"}:
                raise ApiError(f"schema mismatch: {parsed}")
            return usage_detail(data)

        def test_tool_call() -> str:
            data = client.json_request(
                "POST",
                "/chat/completions",
                {
                    "model": args.model,
                    "messages": [{
                        "role": "user",
                        "content": "Use the tool to obtain status of vehicle TEST001.",
                    }],
                    "tools": [{
                        "type": "function",
                        "function": {
                            "name": "get_vehicle_status",
                            "description": "Get status for a vehicle plate",
                            "parameters": {
                                "type": "object",
                                "properties": {"plate": {"type": "string"}},
                                "required": ["plate"],
                                "additionalProperties": False,
                            },
                        },
                    }],
                    "tool_choice": {
                        "type": "function",
                        "function": {"name": "get_vehicle_status"},
                    },
                    "max_tokens": args.max_tokens,
                },
            )
            calls = message_from_completion(data).get("tool_calls") or []
            if not calls:
                raise ApiError("model did not return a tool call")
            function = calls[0].get("function") or {}
            arguments = json.loads(function.get("arguments") or "{}")
            if function.get("name") != "get_vehicle_status" or not arguments.get("plate"):
                raise ApiError(f"invalid tool call: {function}")
            return f"tool={function['name']}, args={arguments}; {usage_detail(data)}"

        def test_streaming() -> str:
            content, usage = client.stream_chat({
                "model": args.model,
                "messages": [{"role": "user", "content": "Reply with STREAM_OK."}],
                "stream": True,
                "temperature": 0,
                "max_tokens": args.max_tokens,
            })
            if "STREAM_OK" not in content:
                raise ApiError(f"unexpected streamed answer: {content[:160]!r}")
            return usage_detail({"usage": usage})

        def test_responses() -> str:
            data = client.json_request(
                "POST",
                "/responses",
                {
                    "model": args.model,
                    "input": "Reply with exactly RESPONSES_OK.",
                    "max_output_tokens": args.max_tokens,
                },
            )
            serialized = json.dumps(data, ensure_ascii=False)
            if "RESPONSES_OK" not in serialized:
                raise ApiError("Responses API returned no expected output")
            return f"object={data.get('object')}, status={data.get('status')}"

        def test_stats() -> str:
            data = client.json_request("GET", "/aitunnel/stats/summary")
            return (
                f"today={data.get('today_spend')} RUB/{data.get('today_requests')} requests, "
                f"month={data.get('month_spend')} RUB/{data.get('month_requests')} requests"
            )

        results.append(run_case("Structured JSON output", test_structured_output))
        results.append(run_case("Forced tool call", test_tool_call))
        results.append(run_case("SSE streaming", test_streaming))
        results.append(run_case("Responses API", test_responses))
        results.append(run_case("Usage statistics", test_stats))

    print("\nResults")
    for result in results:
        print(f"[{result.status}] {result.name} ({result.elapsed_s:.2f}s): {result.detail}")

    failures = [result for result in results if result.status == "FAIL"]
    print(f"\nSummary: {len(results) - len(failures)}/{len(results)} checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
