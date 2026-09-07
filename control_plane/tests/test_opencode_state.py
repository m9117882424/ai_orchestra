import pytest

from control_plane.app.opencode_client import (
    OpenCodeClient,
    OpenCodeError,
    extract_last_assistant_text,
    infer_session_state,
)


class _Response:
    def __init__(self, body: str):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return self.body.encode()


def assistant_with_running_tool():
    return [
        {
            "info": {
                "role": "assistant",
                "agent": "department-lead",
                "modelID": "orchestra-lead",
                "time": {"created": 1788517494585},
            },
            "parts": [
                {"type": "step-start"},
                {
                    "type": "tool",
                    "tool": "read",
                    "state": {
                        "status": "running",
                        "input": {"filePath": "/workspace"},
                    },
                },
            ],
        }
    ]


def assistant_completed():
    return [
        {
            "info": {
                "role": "assistant",
                "agent": "department-lead",
                "modelID": "orchestra-lead",
                "finish": "stop",
                "time": {"created": 1788517494585, "completed": 1788517495585},
            },
            "parts": [{"type": "text", "text": "Готовый итоговый отчет"}],
        }
    ]


def assistant_failed_with_partial_text():
    return [
        {
            "info": {
                "role": "assistant",
                "finish": "stop",
                "error": {"name": "ProviderError", "data": {"message": "upstream failed"}},
                "time": {"created": 1788517494585, "completed": 1788517495585},
            },
            "parts": [{"type": "text", "text": "partial result"}],
        }
    ]


def test_running_tool_is_inferred_busy():
    assert infer_session_state(assistant_with_running_tool()) == "busy"


def test_completed_stop_is_inferred_idle():
    assert infer_session_state(assistant_completed()) == "idle"


def test_failed_assistant_is_never_promoted_to_success():
    messages = assistant_failed_with_partial_text()

    assert infer_session_state(messages) == "error"
    assert extract_last_assistant_text(messages) == ""


def test_messages_backfill_missing_status_and_expose_tool_progress():
    client = OpenCodeClient("http://opencode", "user", "password")
    responses = {
        "/session/status": {},
        "/session/ses-test/message?limit=50": assistant_with_running_tool(),
    }

    client._request = lambda method, path, payload=None: responses[path]  # type: ignore[method-assign]

    statuses = client.session_statuses()
    messages = client.messages("ses-test")

    assert statuses["ses-test"]["type"] == "busy"
    assert statuses["ses-test"]["inferred"] is True
    assert any(
        part.get("progress_only") and "[tool] read: running" in part.get("text", "")
        for part in messages[0]["parts"]
    )
    assert extract_last_assistant_text(messages) == ""


def test_progress_decoration_does_not_replace_final_result():
    client = OpenCodeClient("http://opencode", "user", "password")
    responses = {
        "/session/status": {},
        "/session/ses-done/message?limit=50": assistant_completed(),
    }
    client._request = lambda method, path, payload=None: responses[path]  # type: ignore[method-assign]

    statuses = client.session_statuses()
    messages = client.messages("ses-done")

    assert statuses["ses-done"]["type"] == "idle"
    assert extract_last_assistant_text(messages) == "Готовый итоговый отчет"
    assert messages[0]["info"]["created_at"].endswith("+00:00")


def test_prompt_async_sends_stable_message_and_part_ids():
    client = OpenCodeClient("http://opencode", "user", "password")
    requests = []
    client._request = lambda method, path, payload=None: requests.append(  # type: ignore[method-assign]
        (method, path, payload)
    )

    client.prompt_async(
        "ses-test",
        "Do the work",
        message_id="msg_orchestra_123",
        part_id="prt_orchestra_123",
    )

    assert requests == [
        (
            "POST",
            "/session/ses-test/prompt_async",
            {
                "agent": "department-lead",
                "parts": [
                    {
                        "id": "prt_orchestra_123",
                        "type": "text",
                        "text": "Do the work",
                    }
                ],
                "messageID": "msg_orchestra_123",
            },
        )
    ]


def test_invalid_opencode_json_is_normalized_as_client_error(monkeypatch):
    client = OpenCodeClient("http://opencode", "user", "password")
    monkeypatch.setattr(
        "control_plane.app.opencode_client.urllib.request.urlopen",
        lambda request, timeout: _Response("not-json"),
    )

    with pytest.raises(OpenCodeError, match="невалидный JSON"):
        client.session_statuses()
