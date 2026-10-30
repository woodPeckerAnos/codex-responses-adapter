"""Orchestration between HTTP requests, Codex turns, and Responses events."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from acodex.exceptions import CodexCancelledError, CodexError

from .backend import CodexBackend, translate_codex_error
from .errors import (
    AdapterError,
    codex_timeout,
    codex_turn_failed,
    concurrency_limit_exceeded,
    internal_error,
    previous_response_not_found,
    thread_busy,
)
from .protocol import ResponseBuilder
from .schemas import PreparedRequest
from .settings import Settings
from .store import ResponseStore

logger = logging.getLogger(__name__)


class ConcurrencyLimiter:
    """Bound the number of simultaneously running Codex child processes."""

    def __init__(self, *, max_concurrency: int, queue_timeout: float) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_concurrency = max_concurrency
        self._queue_timeout = queue_timeout

    async def acquire(self) -> None:
        if self._queue_timeout <= 0:
            if self._semaphore.locked():
                raise concurrency_limit_exceeded(self._max_concurrency, self._queue_timeout)
            await self._semaphore.acquire()
            return
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._queue_timeout)
        except TimeoutError as exc:
            raise concurrency_limit_exceeded(
                self._max_concurrency,
                self._queue_timeout,
            ) from exc

    def release(self) -> None:
        self._semaphore.release()


@dataclass
class TurnContext:
    """Resources reserved by one request before its stream is started."""

    prepared: PreparedRequest
    response_id: str
    thread_id: str | None
    created_at: int
    signal: asyncio.Event
    release_callback: Callable[[], None]
    _released: bool = field(default=False, init=False, repr=False)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self.release_callback()


class ResponsesService:
    def __init__(
        self,
        *,
        settings: Settings,
        backend: CodexBackend,
        store: ResponseStore,
    ) -> None:
        self._settings = settings
        self._backend = backend
        self._store = store
        self._limiter = ConcurrencyLimiter(
            max_concurrency=settings.max_concurrency,
            queue_timeout=settings.queue_timeout,
        )
        self._busy_threads: set[str] = set()

    async def begin(self, prepared: PreparedRequest) -> TurnContext:
        thread_id: str | None = None
        if prepared.previous_response_id is not None:
            thread_id = self._store.get(prepared.previous_response_id)
            if thread_id is None:
                raise previous_response_not_found(prepared.previous_response_id)

        await self._limiter.acquire()
        try:
            if thread_id is not None and thread_id in self._busy_threads:
                raise thread_busy(thread_id)
            if thread_id is not None:
                self._busy_threads.add(thread_id)
        except BaseException:
            self._limiter.release()
            raise

        def release_resources() -> None:
            if thread_id is not None:
                self._busy_threads.discard(thread_id)
            self._limiter.release()

        return TurnContext(
            prepared=prepared,
            response_id=f"resp_{secrets.token_hex(24)}",
            thread_id=thread_id,
            created_at=int(time.time()),
            signal=asyncio.Event(),
            release_callback=release_resources,
        )

    async def execute(self, context: TurnContext) -> dict[str, Any]:
        builder = self._builder(context)
        turn = None
        try:
            turn = await self._backend.stream_turn(
                prompt=context.prepared.prompt,
                thread_id=context.thread_id,
                signal=context.signal,
            )
            try:
                async for event in self._iter_events(turn, context):
                    builder.handle(event)
            finally:
                await _close_quietly(turn)
                turn = None

            response = builder.build_completed_response()
            self._remember(context, builder)
            return response
        except asyncio.CancelledError:
            raise
        except AdapterError:
            raise
        except CodexError as exc:
            raise translate_codex_error(exc) from exc
        except Exception as exc:  # pragma: no cover - defensive boundary
            logger.exception("Unexpected error while executing a Codex turn")
            raise internal_error() from exc
        finally:
            if turn is not None:
                await _close_quietly(turn)
            context.release()

    async def stream(self, context: TurnContext) -> AsyncIterator[str]:
        builder = self._builder(context)
        turn = None
        try:
            for event in builder.started_events():
                yield event.encode()

            turn = await self._backend.stream_turn(
                prompt=context.prepared.prompt,
                thread_id=context.thread_id,
                signal=context.signal,
            )
            try:
                async for event in self._iter_events(turn, context):
                    for outbound in builder.handle(event):
                        yield outbound.encode()
            finally:
                await _close_quietly(turn)
                turn = None

            response = builder.build_completed_response()
            self._remember(context, builder)
            yield builder.completed_event(response).encode()
        except asyncio.CancelledError:
            raise
        except AdapterError as exc:
            logger.warning("Streaming Codex turn failed: code=%s message=%s", exc.code, exc.message)
            yield builder.error_event(exc).encode()
        except CodexError as exc:
            translated = translate_codex_error(exc)
            logger.warning(
                "Streaming Codex turn failed: code=%s message=%s",
                translated.code,
                translated.message,
            )
            yield builder.error_event(translated).encode()
        except Exception:  # pragma: no cover - defensive boundary
            logger.exception("Unexpected error while streaming a Codex turn")
            yield builder.error_event(internal_error()).encode()
        finally:
            if turn is not None:
                await _close_quietly(turn)
            context.release()

    def _builder(self, context: TurnContext) -> ResponseBuilder:
        return ResponseBuilder(
            response_id=context.response_id,
            model=context.prepared.model,
            created_at=context.created_at,
            previous_response_id=context.prepared.previous_response_id,
            instructions=context.prepared.instructions,
            metadata=context.prepared.metadata,
            store=context.prepared.store,
            temperature=context.prepared.temperature,
            top_p=context.prepared.top_p,
            max_output_tokens=context.prepared.max_output_tokens,
            user=context.prepared.user,
        )

    async def _iter_events(
        self,
        turn: Any,
        context: TurnContext,
    ) -> AsyncIterator[Any]:
        timeout = self._settings.request_timeout
        iterator = turn.events.__aiter__()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                context.signal.set()
                raise codex_timeout(timeout)
            try:
                try:
                    event = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
                except StopAsyncIteration:
                    return
                except TimeoutError as exc:
                    context.signal.set()
                    raise codex_timeout(timeout) from exc
                except CodexCancelledError as exc:
                    if context.signal.is_set():
                        raise codex_timeout(timeout) from exc
                    raise codex_turn_failed("Codex turn was cancelled.") from exc
            except AdapterError:
                raise
            yield event

    def _remember(self, context: TurnContext, builder: ResponseBuilder) -> None:
        thread_id = builder.thread_id or context.thread_id
        if thread_id is None:
            raise codex_turn_failed(
                "Codex did not report a thread ID; the session cannot be resumed."
            )
        if context.prepared.store:
            self._store.put(context.response_id, thread_id)


async def _close_quietly(turn: Any) -> None:
    try:
        await turn.aclose()
    except Exception:
        logger.debug("Ignoring error while closing Codex turn", exc_info=True)
