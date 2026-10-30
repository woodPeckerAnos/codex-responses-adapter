"""HTTP routes and authentication."""

from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .errors import authentication_error
from .schemas import ResponsesRequest

router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def check_auth(request: Request) -> None:
    """Validate an optional static Bearer token."""
    settings = request.app.state.settings
    expected = settings.api_key_value
    if expected is None:
        return

    header = request.headers.get("authorization")
    if not header:
        raise authentication_error()
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise authentication_error()
    if not secrets.compare_digest(parts[1].encode("utf-8"), expected.encode("utf-8")):
        raise authentication_error()


@router.post("/v1/responses", response_model=None)
async def create_response(
    payload: ResponsesRequest,
    request: Request,
    _: None = Depends(check_auth),
) -> JSONResponse | StreamingResponse:
    settings = request.app.state.settings
    service = request.app.state.service
    prepared = payload.prepare(settings)
    context = await service.begin(prepared)

    if not prepared.stream:
        response = await service.execute(context)
        return JSONResponse(content=response)

    try:
        return StreamingResponse(
            service.stream(context),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )
    except BaseException:
        context.release()
        raise


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    codex_path = settings.codex_path or shutil.which("codex")
    if settings.codex_path and "/" not in settings.codex_path and "\\" not in settings.codex_path:
        codex_path = shutil.which(settings.codex_path)
    resolved_path = Path(codex_path).expanduser() if codex_path else None
    available = bool(
        resolved_path
        and resolved_path.is_file()
        and os.access(resolved_path, os.X_OK)
    )

    return {
        "status": "ok" if available else "degraded",
        "service": "codex-responses-adapter",
        "version": __version__,
        "codex": {
            "available": available,
            "path": str(resolved_path) if resolved_path is not None else None,
        },
        "stored_responses": request.app.state.store.count(),
        "concurrency_limit": settings.max_concurrency,
        "auth_required": settings.api_key_value is not None,
    }
