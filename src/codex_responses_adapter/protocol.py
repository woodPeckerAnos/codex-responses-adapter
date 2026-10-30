"""Pure conversion from acodex events to OpenAI Responses wire objects."""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from acodex.types.events import (
    ItemCompletedEvent,
    ThreadErrorEvent,
    ThreadEvent,
    ThreadStartedEvent,
    TurnCompletedEvent,
    TurnFailedEvent,
    TurnStartedEvent,
    Usage,
)
from acodex.types.items import AgentMessageItem

from .errors import AdapterError, codex_turn_failed
from .sse import SSEEvent

logger = logging.getLogger(__name__)


class ResponseBuilder:
    """Build one Responses API response and its streaming event sequence."""

    def __init__(
        self,
        *,
        response_id: str,
        model: str,
        created_at: int,
        previous_response_id: str | None,
        instructions: str | None,
        metadata: dict[str, str],
        store: bool,
        temperature: float,
        top_p: float,
        max_output_tokens: int | None,
        user: str | None,
    ) -> None:
        self.response_id = response_id
        self.model = model
        self.created_at = created_at
        self.previous_response_id = previous_response_id
        self.instructions = instructions
        self.metadata = metadata
        self.store = store
        self.temperature = temperature
        self.top_p = top_p
        self.max_output_tokens = max_output_tokens
        self.user = user

        self._sequence = 0
        self._output: list[dict[str, Any]] = []
        self._usage: dict[str, Any] | None = None
        self._thread_id: str | None = None
        self._turn_completed = False
        self._completed = False

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def turn_completed(self) -> bool:
        return self._turn_completed

    @property
    def output(self) -> list[dict[str, Any]]:
        return list(self._output)

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def started_events(self) -> list[SSEEvent]:
        """Return the two initial lifecycle events."""
        response = self.response_object("in_progress")
        return [
            SSEEvent(
                "response.created",
                {
                    "type": "response.created",
                    "response": response,
                    "sequence_number": self.next_sequence(),
                },
            ),
            SSEEvent(
                "response.in_progress",
                {
                    "type": "response.in_progress",
                    "response": self.response_object("in_progress"),
                    "sequence_number": self.next_sequence(),
                },
            ),
        ]

    def handle(self, event: ThreadEvent) -> list[SSEEvent]:
        """Convert one acodex event into zero or more Responses SSE events."""
        if isinstance(event, ThreadStartedEvent):
            self._thread_id = event.thread_id
            return []

        if isinstance(event, TurnStartedEvent):
            return []

        if isinstance(event, TurnCompletedEvent):
            self._usage = _usage_payload(event.usage)
            self._turn_completed = True
            return []

        if isinstance(event, ItemCompletedEvent):
            if isinstance(event.item, AgentMessageItem):
                return self._agent_message_events(event.item.text)
            logger.debug(
                "Filtering non-text Codex item type=%s id=%s",
                getattr(event.item, "type", "unknown"),
                getattr(event.item, "id", "unknown"),
            )
            return []

        if isinstance(event, TurnFailedEvent):
            raise codex_turn_failed(event.error.message)

        if isinstance(event, ThreadErrorEvent):
            raise codex_turn_failed(event.message)

        logger.debug("Ignoring unsupported Codex event type=%s", getattr(event, "type", "unknown"))
        return []

    def build_completed_response(self) -> dict[str, Any]:
        if not self._turn_completed:
            raise _missing_completion_error()
        if self._completed:
            raise RuntimeError("response was already completed")
        self._completed = True
        return self.response_object("completed")

    def completed_event(self, response: dict[str, Any]) -> SSEEvent:
        return SSEEvent(
            "response.completed",
            {
                "type": "response.completed",
                "response": response,
                "sequence_number": self.next_sequence(),
            },
        )

    def error_event(self, error: AdapterError) -> SSEEvent:
        return SSEEvent(
            "error",
            {
                "type": "error",
                "code": error.code,
                "message": error.message,
                "param": error.param,
                "sequence_number": self.next_sequence(),
            },
        )

    def response_object(self, status: str) -> dict[str, Any]:
        completed = status == "completed"
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "completed_at": int(time.time()) if completed else None,
            "status": status,
            "error": None,
            "incomplete_details": None,
            "instructions": self.instructions,
            "max_output_tokens": self.max_output_tokens,
            "max_tool_calls": None,
            "model": self.model,
            "output": self._output if completed else [],
            "parallel_tool_calls": True,
            "previous_response_id": self.previous_response_id,
            "prompt_cache_key": None,
            "reasoning": {"effort": None, "summary": None},
            "safety_identifier": None,
            "service_tier": "default",
            "store": self.store,
            "temperature": self.temperature,
            "text": {"format": {"type": "text"}},
            "tool_choice": "auto",
            "tools": [],
            "top_logprobs": 0,
            "top_p": self.top_p,
            "truncation": "disabled",
            "usage": self._usage if completed else None,
            "user": self.user,
            "metadata": dict(self.metadata),
            "background": False,
            "access_programs": None,
            "conversation": None,
        }

    def _agent_message_events(self, text: str) -> list[SSEEvent]:
        item_id = f"msg_{secrets.token_hex(24)}"
        output_index = len(self._output)
        in_progress_item = _message_item(item_id, status="in_progress", text=None)
        completed_item = _message_item(item_id, status="completed", text=text)
        part = _output_text_part(text)

        events = [
            SSEEvent(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": in_progress_item,
                    "sequence_number": self.next_sequence(),
                },
            ),
            SSEEvent(
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "part": _output_text_part(""),
                    "sequence_number": self.next_sequence(),
                },
            ),
            SSEEvent(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "delta": text,
                    "logprobs": [],
                    "sequence_number": self.next_sequence(),
                },
            ),
            SSEEvent(
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "text": text,
                    "logprobs": [],
                    "sequence_number": self.next_sequence(),
                },
            ),
            SSEEvent(
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "part": part,
                    "sequence_number": self.next_sequence(),
                },
            ),
            SSEEvent(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": completed_item,
                    "sequence_number": self.next_sequence(),
                },
            ),
        ]
        self._output.append(completed_item)
        return events


def _message_item(item_id: str, *, status: str, text: str | None) -> dict[str, Any]:
    content: list[dict[str, Any]] = [] if text is None else [_output_text_part(text)]
    return {
        "id": item_id,
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": content,
    }


def _output_text_part(text: str) -> dict[str, Any]:
    return {
        "type": "output_text",
        "text": text,
        "annotations": [],
        "logprobs": [],
    }


def _usage_payload(usage: Usage) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "input_tokens_details": {
            "cached_tokens": usage.cached_input_tokens,
            # acodex 0.117 does not expose cache-write tokens.
            "cache_write_tokens": 0,
        },
        "output_tokens": usage.output_tokens,
        "output_tokens_details": {
            # acodex 0.117 does not expose reasoning token counts.
            "reasoning_tokens": 0
        },
        "total_tokens": usage.input_tokens + usage.output_tokens,
    }


def _missing_completion_error() -> AdapterError:
    from .errors import codex_protocol_error

    return codex_protocol_error(
        "Codex event stream ended without a turn.completed event; "
        "the turn may have been interrupted."
    )
