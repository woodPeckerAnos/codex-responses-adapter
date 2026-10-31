from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from acodex.exceptions import CodexCancelledError
from acodex.types.events import (
    ItemCompletedEvent,
    ThreadEvent,
    ThreadStartedEvent,
    TurnCompletedEvent,
    Usage,
)
from acodex.types.items import AgentMessageItem

from codex_responses_adapter.backend import TurnStream
from codex_responses_adapter.settings import Settings


def make_events(
    text: str = "Hello from Codex.",
    *,
    thread_id: str = "thread-test-1",
    usage: Usage | None = None,
) -> list[ThreadEvent]:
    return [
        ThreadStartedEvent(thread_id=thread_id),
        ItemCompletedEvent(AgentMessageItem(id="item-1", text=text)),
        TurnCompletedEvent(
            usage=usage or Usage(input_tokens=12, cached_input_tokens=2, output_tokens=5)
        ),
    ]


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "log_requests": False,
        "request_timeout": 5.0,
        "queue_timeout": 1.0,
        "max_concurrency": 2,
    }
    values.update(overrides)
    return Settings(**values)


class FakeBackend:
    """An in-memory CodexBackend that records calls and yields scripted events."""

    def __init__(
        self,
        events: list[ThreadEvent] | None = None,
        *,
        delay: float = 0.0,
        error: BaseException | None = None,
    ) -> None:
        self.events = events if events is not None else make_events()
        self.delay = delay
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def stream_turn(
        self,
        *,
        prompt: str,
        thread_id: str | None,
        signal: asyncio.Event,
        output_schema: dict[str, Any] | None = None,
    ) -> TurnStream:
        self.calls.append(
            {
                "prompt": prompt,
                "thread_id": thread_id,
                "output_schema": output_schema,
            }
        )
        if self.error is not None:
            raise self.error

        async def generate() -> AsyncIterator[ThreadEvent]:
            for event in self.events:
                if self.delay:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(signal.wait(), timeout=self.delay)
                    if signal.is_set():
                        raise CodexCancelledError("cancelled")
                yield event

        return TurnStream(events=generate(), initial_thread_id=thread_id)


def parse_sse(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_name: str | None = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if event_name is None or not data_lines:
            continue
        payload = json.loads("\n".join(data_lines))
        payload["_sse_event"] = event_name
        events.append(payload)
    return events
