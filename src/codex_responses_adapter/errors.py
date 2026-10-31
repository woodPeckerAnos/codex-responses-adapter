"""Error types and OpenAI-compatible error payloads."""

from __future__ import annotations

from typing import Any


class AdapterError(Exception):
    """An error that can be rendered as an OpenAI-style API error."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        error_type: str = "server_error",
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.error_type = error_type
        self.param = param
        self.headers = headers or {}

    def as_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }


def invalid_request(message: str, *, param: str | None = None) -> AdapterError:
    return AdapterError(
        status_code=400,
        code="invalid_request",
        message=message,
        error_type="invalid_request_error",
        param=param,
    )


def unsupported_parameter(message: str, *, param: str) -> AdapterError:
    return AdapterError(
        status_code=400,
        code="unsupported_parameter",
        message=message,
        error_type="invalid_request_error",
        param=param,
    )


def previous_response_not_found(response_id: str) -> AdapterError:
    return AdapterError(
        status_code=400,
        code="previous_response_not_found",
        message=(
            f"Previous response '{response_id}' was not found. It may have expired, "
            "belong to another process, or have been created with store=false."
        ),
        error_type="invalid_request_error",
        param="previous_response_id",
    )


def thread_busy(thread_id: str) -> AdapterError:
    return AdapterError(
        status_code=409,
        code="thread_busy",
        message=f"Codex thread '{thread_id}' is already processing another request.",
        error_type="invalid_request_error",
    )


def concurrency_limit_exceeded(limit: int, timeout: float) -> AdapterError:
    return AdapterError(
        status_code=429,
        code="concurrency_limit_exceeded",
        message=(
            f"All {limit} Codex execution slots are busy and no slot became available "
            f"within {timeout:g} seconds."
        ),
        error_type="rate_limit_error",
    )


def codex_unavailable(message: str) -> AdapterError:
    return AdapterError(
        status_code=503,
        code="codex_unavailable",
        message=message,
        error_type="server_error",
    )


def codex_not_authenticated(message: str) -> AdapterError:
    return AdapterError(
        status_code=503,
        code="codex_not_authenticated",
        message=message,
        error_type="server_error",
    )


def codex_exec_failed(message: str) -> AdapterError:
    return AdapterError(
        status_code=502,
        code="codex_exec_failed",
        message=message,
        error_type="server_error",
    )


def codex_turn_failed(message: str) -> AdapterError:
    return AdapterError(
        status_code=502,
        code="codex_turn_failed",
        message=message,
        error_type="server_error",
    )


def codex_timeout(timeout: float) -> AdapterError:
    return AdapterError(
        status_code=504,
        code="codex_timeout",
        message=f"Codex turn exceeded the configured {timeout:g} second timeout.",
        error_type="server_error",
    )


def codex_tool_call_invalid(message: str) -> AdapterError:
    return AdapterError(
        status_code=502,
        code="codex_tool_call_invalid",
        message=message,
        error_type="server_error",
    )


def codex_protocol_error(message: str) -> AdapterError:
    return AdapterError(
        status_code=502,
        code="codex_protocol_error",
        message=message,
        error_type="server_error",
    )


def codex_error(message: str) -> AdapterError:
    return AdapterError(
        status_code=502,
        code="codex_error",
        message=message,
        error_type="server_error",
    )


def authentication_error(message: str = "Invalid or missing API key.") -> AdapterError:
    return AdapterError(
        status_code=401,
        code="invalid_api_key",
        message=message,
        error_type="authentication_error",
        headers={"WWW-Authenticate": "Bearer"},
    )


def internal_error() -> AdapterError:
    return AdapterError(
        status_code=500,
        code="internal_error",
        message="The adapter encountered an unexpected internal error.",
        error_type="server_error",
    )
