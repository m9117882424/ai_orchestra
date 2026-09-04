from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request


class OpenCodeError(RuntimeError):
    pass


class OpenCodeClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
        }

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
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise OpenCodeError(str(exc)) from exc
        return json.loads(raw) if raw else None

    def create_session(self, title: str) -> dict:
        return self._request("POST", "/session", {"title": title})

    def prompt_async(self, session_id: str, prompt: str) -> None:
        self._request(
            "POST",
            f"/session/{session_id}/prompt_async",
            {"agent": "department-lead", "parts": [{"type": "text", "text": prompt}]},
        )

    def session_statuses(self) -> dict:
        return self._request("GET", "/session/status") or {}

    def messages(self, session_id: str) -> list[dict]:
        return self._request("GET", f"/session/{session_id}/message?limit=50") or []

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
            if part.get("type") == "text" and part.get("text")
        ]
        if chunks:
            return "\n".join(chunks).strip()
    return ""
