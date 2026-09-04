from control_plane.app.opencode_client import (
    OpenCodeClient,
    extract_last_assistant_text,
    infer_session_state,
)


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


def test_running_tool_is_inferred_busy():
    assert infer_session_state(assistant_with_running_tool()) == "busy"


def test_completed_stop_is_inferred_idle():
    assert infer_session_state(assistant_completed()) == "idle"


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
