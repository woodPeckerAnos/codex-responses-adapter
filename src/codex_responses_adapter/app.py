"""FastAPI application factory and OpenAI-style exception handling."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .backend import AcodexBackend, CodexBackend
from .engine import ResponsesService
from .errors import AdapterError, internal_error, invalid_request
from .http import router
from .settings import Settings
from .store import ResponseStore

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("codex_responses_adapter").setLevel(
        getattr(logging, level.upper(), logging.INFO)
    )


def create_app(
    settings: Settings | None = None,
    backend: CodexBackend | None = None,
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Codex Responses Adapter",
        version=__version__,
        description="OpenAI Responses-compatible gateway for the local Codex CLI.",
    )
    app.state.settings = settings
    app.state.store = ResponseStore(
        ttl_seconds=settings.response_ttl,
        max_entries=settings.max_response_records,
    )
    app.state.backend = backend or AcodexBackend(settings)
    app.state.service = ResponsesService(
        settings=settings,
        backend=app.state.backend,
        store=app.state.store,
    )

    @app.middleware("http")
    async def request_logging(request: Request, call_next: Any) -> Any:
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            if settings.log_requests:
                logger.exception(
                    "Unhandled request failure method=%s path=%s request_id=%s",
                    request.method,
                    request.url.path,
                    request_id,
                )
            raise
        duration_ms = (time.perf_counter() - started) * 1_000
        if settings.log_requests:
            logger.info(
                "%s %s -> %s %.1fms request_id=%s",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                request_id,
            )
        response.headers.setdefault("X-Request-ID", request_id)
        return response

    @app.exception_handler(AdapterError)
    async def adapter_error_handler(request: Request, exc: AdapterError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.as_payload(),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        message = str(first.get("msg", "Invalid request body"))
        if location:
            message = f"{location}: {message}"
        error = invalid_request(message, param=location or None)
        return JSONResponse(status_code=400, content=error.as_payload())

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        code = "not_found" if exc.status_code == 404 else "http_error"
        error_type = "invalid_request_error" if exc.status_code < 500 else "server_error"
        error = AdapterError(
            status_code=exc.status_code,
            code=code,
            message=detail,
            error_type=error_type,
        )
        return JSONResponse(status_code=exc.status_code, content=error.as_payload())

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled adapter error", exc_info=exc)
        error = internal_error()
        return JSONResponse(status_code=error.status_code, content=error.as_payload())

    app.include_router(router)
    return app


app = create_app()
