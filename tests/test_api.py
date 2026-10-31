from __future__ import annotations

from typing import Any

from acodex.types.events import ThreadError, TurnFailedEvent
from conftest import FakeBackend, make_events, make_settings, parse_sse
from fastapi.testclient import TestClient

from codex_responses_adapter.app import create_app
from codex_responses_adapter.errors import codex_unavailable


def client_for(
    backend: FakeBackend | None = None,
    **settings_overrides: Any,
) -> tuple[TestClient, FakeBackend]:
    fake = backend or FakeBackend()
    app = create_app(settings=make_settings(**settings_overrides), backend=fake)
    return TestClient(app), fake


def test_non_streaming_response() -> None:
    client, _ = client_for()
    response = client.post(
        "/v1/responses",
        json={"model": "gpt-test", "input": "Hello"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["model"] == "gpt-test"
    assert body["output"][0]["type"] == "message"
    assert body["output"][0]["content"][0]["text"] == "Hello from Codex."
    assert body["usage"]["input_tokens"] == 12
    assert body["usage"]["input_tokens_details"]["cached_tokens"] == 2
    assert body["usage"]["total_tokens"] == 17
    assert body["store"] is True
    assert body["id"].startswith("resp_")


def test_streaming_event_sequence_and_no_done_marker() -> None:
    client, _ = client_for()
    with client.stream(
        "POST",
        "/v1/responses",
        json={"model": "gpt-test", "input": "Hello", "stream": True},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    assert "[DONE]" not in body
    events = parse_sse(body)
    assert [event["type"] for event in events] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert [event["sequence_number"] for event in events] == list(range(1, 10))
    assert all(event["_sse_event"] == event["type"] for event in events)
    assert events[4]["delta"] == "Hello from Codex."
    assert events[-1]["response"]["status"] == "completed"
    assert events[-1]["response"]["output"][0]["content"][0]["text"] == "Hello from Codex."


def test_previous_response_id_resumes_codex_thread() -> None:
    client, backend = client_for()
    first = client.post("/v1/responses", json={"input": "First turn"})
    assert first.status_code == 200
    response_id = first.json()["id"]

    second = client.post(
        "/v1/responses",
        json={"input": "Second turn", "previous_response_id": response_id},
    )
    assert second.status_code == 200
    assert backend.calls[0]["thread_id"] is None
    assert backend.calls[1]["thread_id"] == "thread-test-1"
    assert second.json()["previous_response_id"] == response_id


def test_unknown_previous_response_id_returns_400() -> None:
    client, backend = client_for()
    response = client.post(
        "/v1/responses",
        json={"input": "Hello", "previous_response_id": "resp_missing"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "previous_response_not_found"
    assert backend.calls == []


def test_store_false_disables_continuation() -> None:
    client, _ = client_for()
    first = client.post("/v1/responses", json={"input": "One-off", "store": False})
    assert first.status_code == 200
    response_id = first.json()["id"]
    second = client.post(
        "/v1/responses",
        json={"input": "Again", "previous_response_id": response_id},
    )
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "previous_response_not_found"


def test_bearer_auth() -> None:
    client, _ = client_for(api_key="secret")
    unauthorized = client.post("/v1/responses", json={"input": "Hello"})
    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert unauthorized.json()["error"]["code"] == "invalid_api_key"

    authorized = client.post(
        "/v1/responses",
        json={"input": "Hello"},
        headers={"Authorization": "Bearer secret"},
    )
    assert authorized.status_code == 200

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["auth_required"] is True


def test_images_are_rejected() -> None:
    client, _ = client_for()
    image = client.post(
        "/v1/responses",
        json={
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": "https://example.com/a.png"}],
                }
            ]
        },
    )
    assert image.status_code == 400
    assert image.json()["error"]["code"] == "unsupported_parameter"




TOOL = {
    "type": "function",
    "name": "bash",
    "description": "Run a shell command.",
    "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}


def test_tool_call_bridge_non_streaming() -> None:
    decision = '{"actions":[{"type":"tool_call","name":"bash","arguments":{"command":"pwd"}}]}'
    client, backend = client_for(FakeBackend(make_events(decision)))
    response = client.post(
        "/v1/responses",
        json={"input": "Where am I?", "tools": [TOOL], "tool_choice": "auto"},
    )
    assert response.status_code == 200
    body = response.json()
    item = body["output"][0]
    assert item["type"] == "function_call"
    assert item["name"] == "bash"
    assert item["arguments"] == '{"command":"pwd"}'
    assert item["call_id"].startswith("call_")
    assert item["status"] == "completed"
    assert backend.calls[0]["output_schema"] is not None
    assert "Available tools" in backend.calls[0]["prompt"]


def test_tool_call_bridge_streaming_events() -> None:
    decision = '{"actions":[{"type":"tool_call","name":"bash","arguments":{"command":"pwd"}}]}'
    client, _ = client_for(FakeBackend(make_events(decision)))
    with client.stream(
        "POST",
        "/v1/responses",
        json={"input": "Where am I?", "tools": [TOOL], "stream": True},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    events = parse_sse(body)
    assert [event["type"] for event in events] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert events[2]["item"]["type"] == "function_call"
    assert events[3]["delta"] == '{"command":"pwd"}'
    assert events[4]["arguments"] == '{"command":"pwd"}'
    assert events[-1]["response"]["output"][0]["type"] == "function_call"


def test_tool_bridge_can_return_a_message() -> None:
    decision = '{"actions":[{"type":"message","text":"No tool needed."}]}'
    client, _ = client_for(FakeBackend(make_events(decision)))
    response = client.post(
        "/v1/responses",
        json={"input": "Hello", "tools": [TOOL]},
    )
    assert response.status_code == 200
    assert response.json()["output"][0]["content"][0]["text"] == "No tool needed."


def test_tool_bridge_rejects_unknown_tool_name() -> None:
    decision = '{"actions":[{"type":"tool_call","name":"nope","arguments":{}}]}'
    client, _ = client_for(FakeBackend(make_events(decision)))
    response = client.post(
        "/v1/responses",
        json={"input": "Hello", "tools": [TOOL]},
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "codex_tool_call_invalid"


def test_models_endpoint() -> None:
    client, _ = client_for(default_model="codex-cli", model_display_name="Local Codex")
    response = client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == "codex-cli"
    assert body["data"][0]["name"] == "Local Codex"
    assert body["data"][0]["context_window"] == 128000
    assert body["data"][0]["max_output_tokens"] == 16384


def test_models_endpoint_unknown_id_returns_404() -> None:
    client, _ = client_for()
    response = client.get("/v1/models/not-configured")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_models_endpoint_requires_auth_when_configured() -> None:
    client, _ = client_for(api_key="secret")
    unauthorized = client.get("/v1/models")
    assert unauthorized.status_code == 401
    authorized = client.get("/v1/models", headers={"Authorization": "Bearer secret"})
    assert authorized.status_code == 200


def test_oversized_prompt_is_compacted_before_codex() -> None:
    client, backend = client_for(max_prompt_chars=10_000)
    response = client.post(
        "/v1/responses",
        json={"input": "x" * 30_000},
    )
    assert response.status_code == 200
    sent_prompt = backend.calls[0]["prompt"]
    assert len(sent_prompt) <= 10_000
    assert "characters of older conversation omitted" in sent_prompt


def test_timeout_non_streaming() -> None:
    client, _ = client_for(FakeBackend(delay=0.3), request_timeout=0.02)
    response = client.post("/v1/responses", json={"input": "Slow"})
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "codex_timeout"


def test_timeout_streaming_emits_error_event() -> None:
    client, _ = client_for(FakeBackend(delay=0.3), request_timeout=0.02)
    with client.stream(
        "POST",
        "/v1/responses",
        json={"input": "Slow", "stream": True},
    ) as response:
        body = "".join(response.iter_text())
    events = parse_sse(body)
    assert [event["type"] for event in events] == [
        "response.created",
        "response.in_progress",
        "error",
    ]
    assert events[-1]["code"] == "codex_timeout"
    assert events[-1]["sequence_number"] == 3


def test_codex_unavailable_non_streaming_and_streaming() -> None:
    backend = FakeBackend(error=codex_unavailable("codex missing"))
    client, _ = client_for(backend)
    non_stream = client.post("/v1/responses", json={"input": "Hello"})
    assert non_stream.status_code == 503
    assert non_stream.json()["error"]["code"] == "codex_unavailable"

    with client.stream(
        "POST",
        "/v1/responses",
        json={"input": "Hello", "stream": True},
    ) as response:
        body = "".join(response.iter_text())
    events = parse_sse(body)
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "codex_unavailable"


def test_turn_failed_error_path() -> None:
    events = [
        make_events()[0],
        TurnFailedEvent(error=ThreadError(message="bad things")),
    ]
    client, _ = client_for(FakeBackend(events))
    response = client.post("/v1/responses", json={"input": "Hello"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "codex_turn_failed"
    assert response.json()["error"]["message"] == "bad things"


def test_health_degraded_when_codex_missing() -> None:
    client, _ = client_for(codex_path="/definitely/not/here/codex")
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["codex"]["available"] is False
