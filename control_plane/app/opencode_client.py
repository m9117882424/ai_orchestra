from __future__ import annotations

import base64
import copy
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .workspace_protocol import opencode_directory_header


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
    if info.get("error"):
        return "error"

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
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        directory: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
        }
        self.directory = directory
        if directory is not None:
            self.headers["X-OpenCode-Directory"] = opencode_directory_header(directory)
        self._last_statuses: dict = {}

    def for_directory(self, directory: str) -> "OpenCodeClient":
        """Return an isolated client whose every request is bound to one workspace."""
        scoped = copy.copy(self)
        scoped.headers = dict(self.headers)
        scoped.headers["X-OpenCode-Directory"] = opencode_directory_header(directory)
        scoped.directory = directory
        scoped._last_statuses = {}
        return scoped

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
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OpenCodeError("OpenCode вернул невалидный JSON") from exc

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

    def execution_sessions(self, root_session_id: str, *, limit: int = 200) -> list[dict]:
        """Return the root OpenCode session and its direct subagent sessions."""
        result = []
        for session in self.list_sessions(limit=limit):
            session_id = str(session.get("id") or "")
            parent_id = str(session.get("parentID") or "")
            if session_id == root_session_id or parent_id == root_session_id:
                result.append(session)
        return result

    def message(self, session_id: str, message_id: str) -> dict | None:
        try:
            result = self._request("GET", f"/session/{session_id}/message/{message_id}")
        except OpenCodeNotFound:
            return None
        if not isinstance(result, dict):
            raise OpenCodeError("OpenCode message lookup вернул неожиданный формат")
        return result

    def prompt_async(
        self,
        session_id: str,
        prompt: str,
        *,
        message_id: str,
        part_id: str,
    ) -> None:
        payload: dict = {
            "agent": "department-lead",
            "parts": [{"id": part_id, "type": "text", "text": prompt}],
            "messageID": message_id,
        }
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

    def delete_session(self, session_id: str) -> None:
        self._request("DELETE", f"/session/{session_id}")


def detect_stalled_tool_call(
    messages: list[dict],
    *,
    timeout_seconds: int,
    now: datetime | None = None,
) -> dict | None:
    """Return evidence for a running/pending tool call that exceeded its bound.

    Only tool states with an explicit OpenCode ``state.time.start`` timestamp are
    eligible. Long model inference without an active tool call is intentionally
    not classified as stalled here.
    """
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be positive")
    now = now or datetime.now(timezone.utc)
    candidates: list[dict] = []
    for item in messages:
        for part in item.get("parts") or []:
            if part.get("type") != "tool":
                continue
            state = part.get("state") or {}
            status = str(state.get("status") or "").lower()
            if status not in {"pending", "running"}:
                continue
            time_info = state.get("time") or {}
            raw_start = time_info.get("start") if isinstance(time_info, dict) else None
            if not isinstance(raw_start, (int, float)) or isinstance(raw_start, bool):
                continue
            started_at = datetime.fromtimestamp(raw_start / 1000, tz=timezone.utc)
            age_seconds = max(0.0, (now - started_at).total_seconds())
            if age_seconds < timeout_seconds:
                continue
            candidates.append({
                "tool": str(part.get("tool") or "tool"),
                "status": status,
                "started_at": started_at,
                "age_seconds": int(age_seconds),
            })
    if not candidates:
        return None
    return max(candidates, key=lambda item: item["age_seconds"])


def extract_last_assistant_message(messages: list[dict]) -> tuple[str, str] | None:
    """Return the latest successful assistant message id and visible text."""
    for item in reversed(messages):
        info = item.get("info") or {}
        if info.get("role") != "assistant" or info.get("error"):
            continue
        chunks = [
            str(part["text"])
            for part in (item.get("parts") or [])
            if part.get("type") == "text"
            and part.get("text")
            and not part.get("progress_only")
        ]
        if not chunks:
            continue
        message_id = info.get("id")
        if not isinstance(message_id, str) or not message_id:
            return None
        return message_id, "\n".join(chunks).strip()
    return None


def extract_last_assistant_text(messages: list[dict]) -> str:
    for item in reversed(messages):
        info = item.get("info") or {}
        if info.get("role") != "assistant":
            continue
        if info.get("error"):
            return ""
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
