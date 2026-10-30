"""Request parsing and text-only input normalization.

The Responses API input schema is broad. This adapter deliberately accepts only
text message inputs and rejects content types it cannot faithfully forward to
the Codex CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .errors import invalid_request, unsupported_parameter
from .settings import Settings


class TextFormat(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = "text"


class TextOptions(BaseModel):
    model_config = ConfigDict(extra="allow")

    format: TextFormat | None = None


class ResponsesRequest(BaseModel):
    """The subset of the Responses request needed by this adapter.

    Unknown fields are tolerated so that SDK-generated requests can carry
    options this adapter does not use. Fields that would materially change
    behavior are checked in :meth:`prepare`.
    """

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    input: Any
    instructions: str | None = None
    previous_response_id: str | None = None
    stream: bool = False
    store: bool | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    metadata: dict[str, str] | None = None
    user: str | None = None
    tools: list[Any] | None = None
    tool_choice: Any = None
    text: TextOptions | None = None
    background: bool | None = None
    conversation: Any = None

    def prepare(self, settings: Settings) -> PreparedRequest:
        if self.tools:
            raise unsupported_parameter(
                "Tool calling is not supported by this adapter. Remove `tools` from the request.",
                param="tools",
            )
        if self.background is True:
            raise unsupported_parameter(
                "Background responses are not supported by this adapter.",
                param="background",
            )
        if self.conversation is not None:
            raise unsupported_parameter(
                "Conversations are not supported; use `previous_response_id` instead.",
                param="conversation",
            )
        if (
            self.text is not None
            and self.text.format is not None
            and self.text.format.type != "text"
        ):
            raise unsupported_parameter(
                "Only text output is supported; `text.format.type` must be `text`.",
                param="text.format.type",
            )

        prompt = flatten_input(self.input)
        instructions = self.instructions
        if instructions is not None and not instructions.strip():
            instructions = None
        if instructions:
            prompt = f"<instructions>\n{instructions.strip()}\n</instructions>\n\n{prompt}"
        if not prompt.strip():
            raise invalid_request("The request produced an empty prompt.", param="input")

        model = (self.model or "").strip() or settings.default_model
        store = self.store if self.store is not None else settings.store_by_default
        previous_response_id = (self.previous_response_id or "").strip() or None

        return PreparedRequest(
            prompt=prompt,
            stream=self.stream,
            store=store,
            model=model,
            instructions=instructions,
            previous_response_id=previous_response_id,
            metadata=dict(self.metadata or {}),
            temperature=self.temperature if self.temperature is not None else 1.0,
            top_p=self.top_p if self.top_p is not None else 1.0,
            max_output_tokens=self.max_output_tokens,
            user=self.user,
        )


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    """A request normalized for the adapter and Codex backend."""

    prompt: str
    stream: bool
    store: bool
    model: str
    instructions: str | None
    previous_response_id: str | None
    metadata: dict[str, str]
    temperature: float
    top_p: float
    max_output_tokens: int | None
    user: str | None


_ALLOWED_ROLES = {"user", "assistant", "system", "developer"}
_ALLOWED_PART_TYPES = {"input_text", "output_text"}


def flatten_input(raw_input: Any) -> str:
    """Flatten Responses text input into one Codex prompt.

    A single user message is passed through without XML-like wrapper tags.
    Multi-message input is joined with role tags because Codex's stdin prompt
    has no message-role structure of its own.
    """

    if isinstance(raw_input, str):
        return raw_input

    if not isinstance(raw_input, list):
        raise invalid_request(
            "`input` must be a string or an array of text message items.",
            param="input",
        )

    if not raw_input:
        raise invalid_request("`input` must not be an empty array.", param="input")

    rendered_sections: list[str] = []
    for index, raw_item in enumerate(raw_input):
        role, text = _parse_message_item(raw_item, index=index)
        if role == "user":
            rendered_sections.append(text)
        else:
            rendered_sections.append(f"<{role}>\n{text}\n</{role}>")

    if len(rendered_sections) == 1 and _is_single_user_message(raw_input):
        return rendered_sections[0]
    return "\n\n".join(rendered_sections)


def _is_single_user_message(raw_input: list[Any]) -> bool:
    if len(raw_input) != 1:
        return False
    item = raw_input[0]
    return isinstance(item, dict) and item.get("role") == "user"


def _parse_message_item(raw_item: Any, *, index: int) -> tuple[str, str]:
    if not isinstance(raw_item, dict):
        raise invalid_request(
            f"`input[{index}]` must be a message object.",
            param=f"input[{index}]",
        )

    item_type = raw_item.get("type", "message")
    if item_type != "message":
        raise unsupported_parameter(
            f"`input[{index}].type` is {item_type!r}; only text message items are supported.",
            param=f"input[{index}].type",
        )

    role = raw_item.get("role")
    if role not in _ALLOWED_ROLES:
        raise invalid_request(
            f"`input[{index}].role` must be one of: "
            f"{', '.join(sorted(_ALLOWED_ROLES))}.",
            param=f"input[{index}].role",
        )

    if "content" not in raw_item:
        raise invalid_request(
            f"`input[{index}].content` is required.",
            param=f"input[{index}].content",
        )

    content = raw_item["content"]
    if isinstance(content, str):
        return role, content

    if not isinstance(content, list):
        raise invalid_request(
            f"`input[{index}].content` must be a string or an array of text parts.",
            param=f"input[{index}].content",
        )

    parts: list[str] = []
    for part_index, raw_part in enumerate(content):
        if not isinstance(raw_part, dict):
            raise invalid_request(
                f"`input[{index}].content[{part_index}]` must be an object.",
                param=f"input[{index}].content[{part_index}]",
            )
        part_type = raw_part.get("type")
        if part_type not in _ALLOWED_PART_TYPES:
            raise unsupported_parameter(
                f"`input[{index}].content[{part_index}].type` is {part_type!r}; "
                "only input_text/output_text parts are supported.",
                param=f"input[{index}].content[{part_index}].type",
            )
        text = raw_part.get("text")
        if not isinstance(text, str):
            raise invalid_request(
                f"`input[{index}].content[{part_index}].text` must be a string.",
                param=f"input[{index}].content[{part_index}].text",
            )
        parts.append(text)

    return role, "\n".join(parts)
