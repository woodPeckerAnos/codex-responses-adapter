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


def test_tools_and_images_are_rejected() -> None:
    client, _ = client_for()
    tools = client.post(
        "/v1/responses",
        json={"input": "Hello", "tools": [{"type": "function", "name": "x"}]},
    )
    assert tools.status_code == 400
    assert tools.json()["error"]["code"] == "unsupported_parameter"

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
