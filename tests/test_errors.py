from __future__ import annotations

import asyncio

import pytest
from acodex.exceptions import (
    CodexExecError,
    CodexExecutableNotFoundError,
    CodexThreadRunError,
)
from conftest import FakeBackend, make_settings
from fastapi.testclient import TestClient

from codex_responses_adapter.app import create_app
from codex_responses_adapter.backend import translate_codex_error
from codex_responses_adapter.engine import ConcurrencyLimiter
from codex_responses_adapter.errors import AdapterError


def test_translate_missing_executable() -> None:
    error = translate_codex_error(
        CodexExecutableNotFoundError("Could not locate codex", executable_name="codex")
    )
    assert error.status_code == 503
    assert error.code == "codex_unavailable"


def test_translate_authentication_failure() -> None:
    error = translate_codex_error(
        CodexExecError("Codex Exec exited with code 1", stderr="Not logged in")
    )
    assert error.status_code == 503
    assert error.code == "codex_not_authenticated"


def test_translate_exec_failure() -> None:
    error = translate_codex_error(
        CodexExecError("Codex Exec exited with code 1", stderr="something broke")
    )
    assert error.status_code == 502
    assert error.code == "codex_exec_failed"
    assert "something broke" in error.message


def test_translate_thread_run_error() -> None:
    error = translate_codex_error(CodexThreadRunError("bad event"))
    assert error.status_code == 502
    assert error.code == "codex_protocol_error"


def test_concurrency_limiter_timeout() -> None:
    async def scenario() -> None:
        limiter = ConcurrencyLimiter(max_concurrency=1, queue_timeout=0.01)
        await limiter.acquire()
        try:
            with pytest.raises(AdapterError) as exc_info:
                await limiter.acquire()
            assert exc_info.value.code == "concurrency_limit_exceeded"
        finally:
            limiter.release()

    asyncio.run(scenario())


def test_validation_error_uses_openai_error_envelope() -> None:
    app = create_app(settings=make_settings(), backend=FakeBackend())
    client = TestClient(app)
    response = client.post("/v1/responses", json={"input": "hi", "temperature": 99})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_missing_input_returns_400() -> None:
    app = create_app(settings=make_settings(), backend=FakeBackend())
    client = TestClient(app)
    response = client.post("/v1/responses", json={})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_unknown_route_uses_openai_error_envelope() -> None:
    app = create_app(settings=make_settings(), backend=FakeBackend())
    client = TestClient(app)
    response = client.get("/v1/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
