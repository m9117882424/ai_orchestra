from __future__ import annotations

import base64
import copy
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone


class OpenCodeError(RuntimeError):
    pass


class OpenCodeNotFound(OpenCodeError):
    pass


def infer_session_state(messages: list[dict]) -> str:
    """Infer state only when OpenCode omits a session from /session/status.

    The inference is intentionally fail-closed: ambiguous/tool-call states stay busy
    or unknown and are never promoted to idle/success.
    """

    latest_assistant: dict | None = None
    for item in messages:
        info = item.get("info") or {}
        if info.get("role") == "assistant":
            latest_assistant = item
        for part in item.get("parts") or []:
            if part.get("type") != "tool":
                continue
            state = part.get("state") or {}
            if state.get("status") in {"pending", "running"}:
                return "busy"

    if latest_assistant is None:
        return "unknown"

    info = latest_assistant.get("info") or {}
    time_info = info.get("time") or {}
    if not isinstance(time_info, dict) or not time_info.get("completed"):
        return "busy"

    finish = str(info.get("finish") or "").lower()
    if finish in {"stop", "end_turn", "length", "complete", "completed"}:
        return "idle"
    if finish == "tool-calls":
        return "busy"
    return "unknown"


def _normalize_timestamp(info: dict) -> None:
    if info.get("created_at") or info.get("createdAt"):
        return
    time_info = info.get("time") or {}
    if not isinstance(time_info, dict):
        return
    raw = time_info.get("created")
    if isinstance(raw, (int, float)):
        info["created_at"] = datetime.fromtimestamp(raw / 1000, tz=timezone.utc).isoformat()


def _tool_progress_text(part: dict) -> str:
    state = part.get("state") or {}
    status = str(state.get("status") or "unknown")
    tool = str(part.get("tool") or "tool")
    title = state.get("title")
    input_data = state.get("input")
    error = state.get("error")

    pieces = [f"[tool] {tool}: {status}"]
    if title:
        pieces.append(str(title))
    elif input_data:
        try:
            pieces.append(json.dumps(input_data, ensure_ascii=False, sort_keys=True)[:600])
        except TypeError:
            pieces.append(str(input_data)[:600])
    if error:
        pieces.append(f"error: {str(error)[:1200]}")
    return "\n".join(pieces)


def decorate_progress_messages(messages: list[dict]) -> list[dict]:
    """Add manager-facing tool telemetry without changing execution result text."""

    decorated = copy.deepcopy(messages)
    for item in decorated:
        info = item.get("info") or {}
        _normalize_timestamp(info)
        item["info"] = info
        additions = []
        for part in item.get("parts") or []:
            if part.get("type") == "tool":
                additions.append(
                    {
                        "type": "text",
                        "text": _tool_progress_text(part),
                        "progress_only": True,
                    }
                )
        if additions:
            item.setdefault("parts", []).extend(additions)
    return decorated


class OpenCodeClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
        }
        self._last_statuses: dict = {}

    def _request(self, method: str, path: str, payload: dict | None = None):
        data = None
        headers = dict(self.headers)
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise OpenCodeNotFound(str(exc)) from exc
            raise OpenCodeError(str(exc)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise OpenCodeError(str(exc)) from exc
        return json.loads(raw) if raw else None

    def create_session(self, title: str, *, metadata: dict | None = None) -> dict:
        payload: dict = {"title": title}
        if metadata:
            payload["metadata"] = metadata
        result = self._request("POST", "/session", payload)
        if not isinstance(result, dict):
            raise OpenCodeError("OpenCode /session вернул неожиданный формат")
        return result

    def list_sessions(self, *, limit: int = 200) -> list[dict]:
        result = self._request("GET", f"/session?limit={limit}") or []
        if not isinstance(result, list):
            raise OpenCodeError("OpenCode /session вернул неожиданный формат")
        return [item for item in result if isinstance(item, dict)]

    def sessions_for_execution(self, execution_id: str) -> list[dict]:
        matches = []
        for session in self.list_sessions():
            metadata = session.get("metadata") or {}
            if isinstance(metadata, dict) and metadata.get("ai_orchestra_execution_id") == execution_id:
                matches.append(session)
        return matches

    def message(self, session_id: str, message_id: str) -> dict | None:
        try:
            result = self._request("GET", f"/session/{session_id}/message/{message_id}")
        except OpenCodeNotFound:
            return None
        if not isinstance(result, dict):
            raise OpenCodeError("OpenCode message lookup вернул неожиданный формат")
        return result

    def prompt_async(self, session_id: str, prompt: str, *, message_id: str | None = None) -> None:
        payload: dict = {
            "agent": "department-lead",
            "parts": [{"type": "text", "text": prompt}],
        }
        if message_id:
            payload["messageID"] = message_id
        self._request("POST", f"/session/{session_id}/prompt_async", payload)

    def session_statuses(self) -> dict:
        statuses = self._request("GET", "/session/status") or {}
        if not isinstance(statuses, dict):
            raise OpenCodeError("OpenCode /session/status вернул неожиданный формат")
        self._last_statuses = statuses
        return statuses

    def messages(self, session_id: str) -> list[dict]:
        messages = self._request("GET", f"/session/{session_id}/message?limit=50") or []
        if not isinstance(messages, list):
            raise OpenCodeError("OpenCode /session/:id/message вернул неожиданный формат")

        if session_id not in self._last_statuses:
            inferred = infer_session_state(messages)
            if inferred != "unknown":
                self._last_statuses[session_id] = {"type": inferred, "inferred": True}

        return decorate_progress_messages(messages)

    def abort(self, session_id: str) -> None:
        self._request("POST", f"/session/{session_id}/abort", {})


def extract_last_assistant_text(messages: list[dict]) -> str:
    for item in reversed(messages):
        info = item.get("info") or {}
        if info.get("role") != "assistant":
            continue
        chunks = [
            str(part["text"])
            for part in (item.get("parts") or [])
            if part.get("type") == "text"
            and part.get("text")
            and not part.get("progress_only")
        ]
        if chunks:
            return "\n".join(chunks).strip()
    return ""
