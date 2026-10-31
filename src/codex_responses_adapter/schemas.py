"""Request parsing and prompt normalization.

The Responses API input schema is broad. This adapter forwards text messages and
tool-call history, and rejects content it cannot faithfully represent in the
Codex prompt (images, files, unknown item types).
"""

from __future__ import annotations

import json
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
    parallel_tool_calls: bool | None = None
    text: TextOptions | None = None
    background: bool | None = None
    conversation: Any = None

    def prepare(self, settings: Settings) -> PreparedRequest:
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

        tools = _parse_tools(self.tools)
        if self.tool_choice is None:
            tool_choice = None
        elif isinstance(self.tool_choice, str):
            if self.tool_choice not in {"auto", "none", "required"}:
                raise unsupported_parameter(
                    "`tool_choice` must be `auto`, `none`, or `required`.",
                    param="tool_choice",
                )
            tool_choice = self.tool_choice
        else:
            raise unsupported_parameter(
                "Pinned `tool_choice` objects are not supported; use auto/none/required.",
                param="tool_choice",
            )
        if tool_choice == "none":
            tools = ()
        if tool_choice == "required" and not tools:
            raise invalid_request(
                "`tool_choice` is `required`, but no supported tools were provided.",
                param="tool_choice",
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
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=self.parallel_tool_calls is not False,
        )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A function tool supplied by the calling agent framework."""

    name: str
    description: str
    parameters: dict[str, Any]


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
    tools: tuple[ToolSpec, ...]
    tool_choice: str | None
    parallel_tool_calls: bool


_ALLOWED_ROLES = {"user", "assistant", "system", "developer"}
_ALLOWED_PART_TYPES = {"input_text", "output_text", "text"}


def _parse_tools(raw_tools: list[Any] | None) -> tuple[ToolSpec, ...]:
    if not raw_tools:
        return ()

    tools: list[ToolSpec] = []
    names: set[str] = set()
    for index, raw_tool in enumerate(raw_tools):
        if not isinstance(raw_tool, dict):
            raise invalid_request(
                f"`tools[{index}]` must be an object.",
                param=f"tools[{index}]",
            )
        tool_type = raw_tool.get("type", "function")
        if tool_type != "function":
            raise unsupported_parameter(
                f"`tools[{index}].type` is {tool_type!r}; only function tools are supported.",
                param=f"tools[{index}].type",
            )
        name = raw_tool.get("name")
        if not isinstance(name, str) or not name.strip():
            raise invalid_request(
                f"`tools[{index}].name` must be a non-empty string.",
                param=f"tools[{index}].name",
            )
        name = name.strip()
        if name in names:
            raise invalid_request(
                f"Duplicate tool name {name!r}.",
                param=f"tools[{index}].name",
            )
        names.add(name)

        description = raw_tool.get("description", "")
        if description is None:
            description = ""
        if not isinstance(description, str):
            raise invalid_request(
                f"`tools[{index}].description` must be a string.",
                param=f"tools[{index}].description",
            )

        parameters = raw_tool.get("parameters")
        if parameters is None:
            parameters = {"type": "object", "properties": {}}
        if not isinstance(parameters, dict):
            raise invalid_request(
                f"`tools[{index}].parameters` must be a JSON Schema object.",
                param=f"tools[{index}].parameters",
            )
        tools.append(
            ToolSpec(
                name=name,
                description=description,
                parameters=parameters,
            )
        )
    return tuple(tools)


def flatten_input(raw_input: Any) -> str:
    """Flatten Responses text and tool history into one Codex prompt.

    A single user message is passed through without XML-like wrapper tags.
    Multi-message and tool history is joined with explicit tags because Codex's
    stdin prompt has no Responses item structure of its own.
    """

    if isinstance(raw_input, str):
        return raw_input

    if not isinstance(raw_input, list):
        raise invalid_request(
            "`input` must be a string or an array of Responses input items.",
            param="input",
        )

    if not raw_input:
        raise invalid_request("`input` must not be an empty array.", param="input")

    rendered_sections: list[str] = []
    for index, raw_item in enumerate(raw_input):
        if not isinstance(raw_item, dict):
            raise invalid_request(
                f"`input[{index}]` must be an object.",
                param=f"input[{index}]",
            )
        item_type = raw_item.get("type", "message")
        if item_type == "message":
            role, text = _parse_message_item(raw_item, index=index)
            if role == "user":
                rendered_sections.append(text)
            else:
                rendered_sections.append(f"<{role}>\n{text}\n</{role}>")
        elif item_type == "function_call":
            rendered_sections.append(_parse_function_call_item(raw_item, index=index))
        elif item_type == "function_call_output":
            rendered_sections.append(_parse_function_call_output_item(raw_item, index=index))
        elif item_type == "reasoning":
            # Reasoning items are model-internal. The adapter does not emit them,
            # but tolerate them when another provider wrote the history.
            continue
        else:
            raise unsupported_parameter(
                f"`input[{index}].type` is {item_type!r}; "
                "only message, function_call, function_call_output, and reasoning items "
                "are supported.",
                param=f"input[{index}].type",
            )

    if len(rendered_sections) == 1 and _is_single_user_message(raw_input):
        return rendered_sections[0]
    return "\n\n".join(rendered_sections)


def _is_single_user_message(raw_input: list[Any]) -> bool:
    if len(raw_input) != 1:
        return False
    item = raw_input[0]
    return (
        isinstance(item, dict)
        and item.get("type", "message") == "message"
        and item.get("role") == "user"
    )


def _parse_message_item(raw_item: dict[str, Any], *, index: int) -> tuple[str, str]:
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
                "only text content parts are supported.",
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


def _parse_function_call_item(raw_item: dict[str, Any], *, index: int) -> str:
    name = raw_item.get("name")
    if not isinstance(name, str) or not name:
        raise invalid_request(
            f"`input[{index}].name` must be a non-empty string.",
            param=f"input[{index}].name",
        )
    call_id = raw_item.get("call_id") or raw_item.get("id")
    if not isinstance(call_id, str) or not call_id:
        raise invalid_request(
            f"`input[{index}].call_id` is required for a function call.",
            param=f"input[{index}].call_id",
        )
    arguments = raw_item.get("arguments", "{}")
    if isinstance(arguments, dict):
        arguments_text = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(arguments, str):
        arguments_text = arguments
    else:
        raise invalid_request(
            f"`input[{index}].arguments` must be a JSON string or object.",
            param=f"input[{index}].arguments",
        )
    return (
        f'<tool_call name="{name}" call_id="{call_id}">\n'
        f"{arguments_text}\n"
        "</tool_call>"
    )


def _parse_function_call_output_item(raw_item: dict[str, Any], *, index: int) -> str:
    call_id = raw_item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise invalid_request(
            f"`input[{index}].call_id` is required for a function call output.",
            param=f"input[{index}].call_id",
        )
    output = _flatten_tool_output(raw_item.get("output"), index=index)
    return f'<tool_result call_id="{call_id}">\n{output}\n</tool_result>'


def _flatten_tool_output(output: Any, *, index: int) -> str:
    if output is None:
        return "(no tool output)"
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts: list[str] = []
        for raw_part in output:
            if isinstance(raw_part, str):
                parts.append(raw_part)
                continue
            if not isinstance(raw_part, dict):
                parts.append(json.dumps(raw_part, ensure_ascii=False))
                continue
            part_type = raw_part.get("type")
            if part_type in {"input_text", "output_text", "text"}:
                text = raw_part.get("text")
                if isinstance(text, str):
                    parts.append(text)
                continue
            if part_type in {"input_image", "image_url"}:
                # Tool results can contain images (for example DSH read_image).
                # Keep the turn usable by leaving an explicit placeholder rather
                # than failing the whole agent run.
                parts.append("[image tool output omitted: text-only adapter]")
                continue
            parts.append(json.dumps(raw_part, ensure_ascii=False))
        return "\n".join(parts)
    if isinstance(output, dict):
        if output.get("type") in {"input_text", "output_text", "text"}:
            text = output.get("text")
            if isinstance(text, str):
                return text
        return json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    return str(output)
