"""Thin adapter around acodex's asynchronous Codex client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from acodex import AsyncCodex
from acodex.exceptions import (
    CodexCancelledError,
    CodexError,
    CodexExecError,
    CodexExecutableNotFoundError,
    CodexThreadRunError,
)
from acodex.types.events import ThreadEvent

from .errors import (
    AdapterError,
    codex_error,
    codex_exec_failed,
    codex_not_authenticated,
    codex_protocol_error,
    codex_unavailable,
    internal_error,
)
from .settings import Settings

logger = logging.getLogger(__name__)

_STDERR_TAIL_CHARS = 1_200


class CodexBackend(Protocol):
    """Interface used by the service; tests provide an in-memory implementation."""

    async def stream_turn(
        self,
        *,
        prompt: str,
        thread_id: str | None,
        signal: asyncio.Event,
    ) -> TurnStream: ...


@dataclass
class TurnStream:
    """A running or soon-to-run Codex turn."""

    events: AsyncIterator[ThreadEvent]
    initial_thread_id: str | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self.events, "aclose", None)
        if close is None:
            return
        try:
            await close()
        except (RuntimeError, GeneratorExit):
            logger.debug("Ignoring error while closing Codex event iterator", exc_info=True)


class AcodexBackend:
    """Run Codex turns through ``AsyncCodex`` / ``AsyncThread.run_streamed``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncCodex | None = None

    async def stream_turn(
        self,
        *,
        prompt: str,
        thread_id: str | None,
        signal: asyncio.Event,
    ) -> TurnStream:
        try:
            client = self._get_client()
            thread_options = self._thread_options()
            if thread_id is None:
                thread = client.start_thread(**thread_options)
            else:
                thread = client.resume_thread(thread_id, **thread_options)
            streamed = await thread.run_streamed(prompt, signal=signal)
        except CodexError as exc:
            raise translate_codex_error(exc) from exc
        except OSError as exc:
            raise codex_unavailable(f"Could not start the Codex CLI: {exc}") from exc
        except Exception as exc:  # pragma: no cover - defensive boundary
            logger.exception("Unexpected error while starting a Codex turn")
            raise internal_error() from exc

        return TurnStream(events=streamed.events, initial_thread_id=thread_id)

    def _get_client(self) -> AsyncCodex:
        if self._client is None:
            self._client = AsyncCodex(
                codex_path_override=self._settings.codex_path,
            )
        return self._client

    def _thread_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "sandbox_mode": self._settings.codex_sandbox,
            "approval_policy": self._settings.codex_approval_policy,
            "skip_git_repo_check": self._settings.skip_git_repo_check,
            "web_search_mode": "live" if self._settings.web_search else "disabled",
            "network_access_enabled": False,
            "working_directory": str(self._settings.effective_workdir),
        }
        if self._settings.codex_model:
            options["model"] = self._settings.codex_model
        return options


def translate_codex_error(exc: BaseException) -> AdapterError:
    """Translate an acodex exception without leaking unbounded process output."""

    if isinstance(exc, CodexExecutableNotFoundError):
        return codex_unavailable(
            "The Codex CLI was not found on PATH. Install it and run `codex --version`, "
            "or set CRA_CODEX_PATH to the executable."
        )

    if isinstance(exc, CodexCancelledError):
        return codex_error("The Codex turn was cancelled.")

    if isinstance(exc, CodexExecError):
        stderr = _tail(exc.stderr or "")
        stdout = _tail(exc.stdout or "")
        combined = f"{stderr}\n{stdout}\n{exc}".lower()
        if _looks_like_auth_failure(combined):
            return codex_not_authenticated(
                "Codex CLI is not authenticated. Run `codex login` and retry. "
                f"Codex said: {stderr or 'authentication required'}"
            )
        if "spawn failure" in combined or "no such file" in combined:
            return codex_unavailable(
                "Could not start the Codex CLI executable. Check CRA_CODEX_PATH and "
                f"permissions. Codex said: {stderr or str(exc)}"
            )
        headline = str(exc).splitlines()[0].strip() or "Codex CLI exited unsuccessfully"
        detail = stderr or "no stderr output"
        return codex_exec_failed(f"{headline}. stderr: {_tail(detail)}")

    if isinstance(exc, CodexThreadRunError):
        return codex_protocol_error(f"Codex turn failed: {exc}")

    if isinstance(exc, CodexError):
        return codex_error(str(exc))

    return internal_error()


def _looks_like_auth_failure(text: str) -> bool:
    markers = (
        "not logged in",
        "login required",
        "please log in",
        "authentication required",
        "unauthorized",
        "401",
    )
    return any(marker in text for marker in markers)


def _tail(value: str) -> str:
    stripped = value.strip()
    if len(stripped) <= _STDERR_TAIL_CHARS:
        return stripped
    return "..." + stripped[-_STDERR_TAIL_CHARS:]
