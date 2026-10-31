from __future__ import annotations

import asyncio
import os
from pathlib import Path

from acodex.types.events import (
    ItemCompletedEvent,
    ThreadStartedEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
)
from acodex.types.items import AgentMessageItem
from conftest import make_settings

from codex_responses_adapter.backend import AcodexBackend
from codex_responses_adapter.engine import ResponsesService
from codex_responses_adapter.errors import AdapterError
from codex_responses_adapter.schemas import ResponsesRequest
from codex_responses_adapter.store import ResponseStore

FAKE_CODEX = """#!/usr/bin/env python3
import json
import sys

sys.stdin.read()
args = sys.argv[1:]
thread_id = "thread-new"
if "resume" in args:
    thread_id = args[args.index("resume") + 1]
text = (
    json.dumps({"actions": [{"type": "message", "text": "schema decision"}]})
    if "--output-schema" in args
    else f"backend {thread_id}"
)

print(json.dumps({"type": "thread.started", "thread_id": thread_id}))
print(json.dumps({"type": "turn.started"}))
print(json.dumps({
    "type": "item.completed",
    "item": {
        "id": "item_1",
        "type": "agent_message",
        "text": text,
    },
}))
print(json.dumps({
    "type": "turn.completed",
    "usage": {
        "input_tokens": 7,
        "cached_input_tokens": 1,
        "output_tokens": 2,
    },
}))
"""


def _write_fake_codex(tmp_path: Path) -> Path:
    script = tmp_path / "codex"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    script.chmod(script.stat().st_mode | 0o111)
    return script


def test_acodex_backend_new_and_resumed_thread(tmp_path: Path) -> None:
    script = _write_fake_codex(tmp_path)
    backend = AcodexBackend(make_settings(codex_path=str(script)))

    async def scenario() -> None:
        signal = asyncio.Event()
        first = await backend.stream_turn(prompt="hello", thread_id=None, signal=signal)
        first_events = [event async for event in first.events]
        await first.aclose()
        assert isinstance(first_events[0], ThreadStartedEvent)
        assert first_events[0].thread_id == "thread-new"
        assert isinstance(first_events[1], TurnStartedEvent)
        assert isinstance(first_events[2], ItemCompletedEvent)
        assert isinstance(first_events[2].item, AgentMessageItem)
        assert first_events[2].item.text == "backend thread-new"
        assert isinstance(first_events[3], TurnCompletedEvent)
        assert first_events[3].usage.output_tokens == 2

        second = await backend.stream_turn(
            prompt="again",
            thread_id="thread-existing",
            signal=signal,
        )
        second_events = [event async for event in second.events]
        await second.aclose()
        assert isinstance(second_events[0], ThreadStartedEvent)
        assert second_events[0].thread_id == "thread-existing"
        assert isinstance(second_events[1], TurnStartedEvent)
        assert isinstance(second_events[2], ItemCompletedEvent)
        assert isinstance(second_events[2].item, AgentMessageItem)
        assert second_events[2].item.text == "backend thread-existing"

    asyncio.run(scenario())


def test_acodex_backend_passes_output_schema(tmp_path: Path) -> None:
    script = _write_fake_codex(tmp_path)
    backend = AcodexBackend(make_settings(codex_path=str(script)))

    async def scenario() -> None:
        signal = asyncio.Event()
        stream = await backend.stream_turn(
            prompt="choose",
            thread_id=None,
            signal=signal,
            output_schema={"type": "object"},
        )
        events = [event async for event in stream.events]
        await stream.aclose()
        assert isinstance(events[1], TurnStartedEvent)
        assert isinstance(events[2].item, AgentMessageItem)
        assert events[2].item.text == (
            '{"actions": [{"type": "message", "text": "schema decision"}]}'
        )

    asyncio.run(scenario())


def test_service_translates_missing_executable(tmp_path: Path) -> None:
    settings = make_settings(codex_path=str(tmp_path / "missing-codex"))
    service = ResponsesService(
        settings=settings,
        backend=AcodexBackend(settings),
        store=ResponseStore(ttl_seconds=60, max_entries=10),
    )

    async def scenario() -> None:
        prepared = ResponsesRequest(input="hello").prepare(settings)
        context = await service.begin(prepared)
        try:
            await service.execute(context)
        except AdapterError as exc:
            assert exc.code == "codex_unavailable"
        else:  # pragma: no cover - the missing path must fail
            raise AssertionError("expected a missing-executable error")

    asyncio.run(scenario())


def test_fake_codex_is_executable(tmp_path: Path) -> None:
    script = _write_fake_codex(tmp_path)
    assert os.access(script, os.X_OK)
