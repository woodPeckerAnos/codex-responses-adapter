from __future__ import annotations

import pytest
from acodex.types.events import (
    ItemCompletedEvent,
    ThreadError,
    ThreadErrorEvent,
    ThreadStartedEvent,
    TurnCompletedEvent,
    TurnFailedEvent,
    Usage,
)
from acodex.types.items import AgentMessageItem, ReasoningItem

from codex_responses_adapter.errors import AdapterError
from codex_responses_adapter.protocol import ResponseBuilder


def make_builder(*, store: bool = True) -> ResponseBuilder:
    return ResponseBuilder(
        response_id="resp_test",
        model="gpt-test",
        created_at=1_700_000_000,
        previous_response_id="resp_prev",
        instructions="Be brief.",
        metadata={"tenant": "test"},
        store=store,
        temperature=1.0,
        top_p=1.0,
        max_output_tokens=None,
        user="user-1",
    )


def test_created_and_in_progress_events_start_at_one() -> None:
    builder = make_builder()
    events = builder.started_events()
    assert [event.event for event in events] == ["response.created", "response.in_progress"]
    assert [event.data["sequence_number"] for event in events] == [1, 2]
    assert events[0].data["response"]["status"] == "in_progress"
    assert events[0].data["response"]["output"] == []
    assert events[0].data["response"]["previous_response_id"] == "resp_prev"


def test_agent_message_maps_to_delta_and_item_lifecycle() -> None:
    builder = make_builder()
    builder.started_events()
    assert builder.handle(ThreadStartedEvent(thread_id="thread-123")) == []
    assert builder.thread_id == "thread-123"

    events = builder.handle(
        ItemCompletedEvent(AgentMessageItem(id="item-1", text="Hello world"))
    )
    assert [event.event for event in events] == [
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
    ]
    assert [event.data["sequence_number"] for event in events] == [3, 4, 5, 6, 7, 8]
    assert events[2].data["delta"] == "Hello world"
    assert events[2].data["logprobs"] == []
    assert builder.output[0]["content"][0]["text"] == "Hello world"

    assert builder.handle(
        TurnCompletedEvent(usage=Usage(input_tokens=12, cached_input_tokens=2, output_tokens=5))
    ) == []

    response = builder.build_completed_response()
    assert response["status"] == "completed"
    assert response["output"][0]["content"][0]["text"] == "Hello world"
    assert response["usage"] == {
        "input_tokens": 12,
        "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 0},
        "output_tokens": 5,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 17,
    }

    completed = builder.completed_event(response)
    assert completed.event == "response.completed"
    assert completed.data["sequence_number"] == 9


def test_non_text_items_are_filtered() -> None:
    builder = make_builder()
    builder.started_events()
    builder.handle(TurnCompletedEvent(usage=Usage(1, 0, 1)))
    events = builder.handle(
        ItemCompletedEvent(ReasoningItem(id="reasoning-1", text="private chain"))
    )
    assert events == []
    assert builder.output == []


def test_multiple_agent_messages_become_multiple_output_items() -> None:
    builder = make_builder()
    builder.started_events()
    builder.handle(ItemCompletedEvent(AgentMessageItem(id="item-1", text="first")))
    builder.handle(ItemCompletedEvent(AgentMessageItem(id="item-2", text="second")))
    builder.handle(TurnCompletedEvent(usage=Usage(2, 0, 2)))
    response = builder.build_completed_response()
    assert [item["content"][0]["text"] for item in response["output"]] == ["first", "second"]
    assert len({item["id"] for item in response["output"]}) == 2


def test_turn_failed_raises_adapter_error() -> None:
    builder = make_builder()
    with pytest.raises(AdapterError) as exc_info:
        builder.handle(TurnFailedEvent(error=ThreadError(message="model failed")))
    assert exc_info.value.code == "codex_turn_failed"
    assert exc_info.value.message == "model failed"


def test_thread_error_event_raises_adapter_error() -> None:
    builder = make_builder()
    with pytest.raises(AdapterError) as exc_info:
        builder.handle(ThreadErrorEvent(message="stream broke"))
    assert exc_info.value.code == "codex_turn_failed"


def test_missing_turn_completed_is_protocol_error() -> None:
    builder = make_builder()
    builder.started_events()
    builder.handle(ItemCompletedEvent(AgentMessageItem(id="item-1", text="hello")))
    with pytest.raises(AdapterError) as exc_info:
        builder.build_completed_response()
    assert exc_info.value.code == "codex_protocol_error"


def test_error_event_has_next_sequence_number() -> None:
    builder = make_builder()
    builder.started_events()
    error = AdapterError(
        status_code=504,
        code="codex_timeout",
        message="timed out",
        error_type="server_error",
    )
    event = builder.error_event(error)
    assert event.event == "error"
    assert event.data == {
        "type": "error",
        "code": "codex_timeout",
        "message": "timed out",
        "param": None,
        "sequence_number": 3,
    }
