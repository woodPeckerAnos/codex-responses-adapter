"""Environment-backed configuration for the adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from ``CRA_*`` environment variables or a ``.env`` file."""

    model_config = SettingsConfigDict(
        env_prefix="CRA_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"

    codex_path: str | None = None
    codex_model: str | None = None
    codex_workdir: Path | None = None
    codex_sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "read-only"
    codex_approval_policy: Literal["never", "on-request", "on-failure", "untrusted"] = "never"
    web_search: bool = False
    skip_git_repo_check: bool = True

    request_timeout: float = Field(default=300.0, gt=0)
    queue_timeout: float = Field(default=30.0, ge=0)
    max_concurrency: int = Field(default=2, ge=1)

    response_ttl: int = Field(default=86_400, ge=1)
    max_response_records: int = Field(default=1_000, ge=1)
    store_by_default: bool = True

    api_key: SecretStr | None = None
    log_requests: bool = True
    log_prompts: bool = False
    default_model: str = "codex-cli"
    model_display_name: str = "Codex CLI"
    model_context_window: int = Field(default=128_000, ge=1)
    model_max_output_tokens: int = Field(default=16_384, ge=1)

    @field_validator("codex_path", "codex_model", mode="before")
    @classmethod
    def _empty_string_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("codex_path", mode="after")
    @classmethod
    def _expand_codex_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return str(Path(value).expanduser())

    @field_validator("default_model", mode="before")
    @classmethod
    def _default_model_name(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return "codex-cli"
        return value

    @field_validator("api_key", mode="before")
    @classmethod
    def _empty_api_key_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, SecretStr) and not value.get_secret_value().strip():
            return None
        return value

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return normalized

    @field_validator("codex_workdir")
    @classmethod
    def _normalize_workdir(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        expanded = value.expanduser()
        if not expanded.exists():
            raise ValueError(f"codex_workdir does not exist: {expanded}")
        if not expanded.is_dir():
            raise ValueError(f"codex_workdir is not a directory: {expanded}")
        return expanded

    @property
    def api_key_value(self) -> str | None:
        if self.api_key is None:
            return None
        return self.api_key.get_secret_value()

    @property
    def effective_workdir(self) -> Path:
        return self.codex_workdir if self.codex_workdir is not None else Path.cwd()
