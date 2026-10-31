"""Prompt and JSON-schema bridge for caller-supplied function tools.

Codex CLI is an agent, not a general function-calling transport. To let an
external harness execute its own tools, the adapter asks Codex for one
structured decision per turn and converts that decision into Responses
``message`` / ``function_call`` output items. The external harness executes the
tool and sends the result back in the next request.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from .errors import codex_protocol_error, codex_tool_call_invalid
from .schemas import PreparedRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DecisionAction:
    kind: Literal["message", "tool_call"]
    text: str = ""
    name: str = ""
    arguments: str = "{}"


def build_tool_prompt(prepared: PreparedRequest) -> str:
    """Prepend the tool-selection protocol to the normalized conversation."""
    protocol = _tool_protocol(prepared)
    return f"{protocol}\n\n{prepared.prompt}"


def decision_schema(prepared: PreparedRequest) -> dict[str, Any]:
    """JSON Schema passed to Codex via ``--output-schema``."""
    action_properties: dict[str, Any] = {
        "type": {"type": "string", "enum": ["message", "tool_call"]},
        "text": {"type": "string"},
        "name": {"type": "string"},
        # Codex's strict output schema cannot describe arbitrary tool-argument
        # properties. The model therefore returns a JSON-encoded object here.
        "arguments": {"type": "string"},
    }
    action_schema: dict[str, Any] = {
        "type": "object",
        "properties": action_properties,
        "required": ["type", "text", "name", "arguments"],
        "additionalProperties": False,
    }
    actions_schema: dict[str, Any] = {
        "type": "array",
        "minItems": 1,
        "items": action_schema,
    }
    if not prepared.parallel_tool_calls:
        actions_schema["maxItems"] = 1
    return {
        "type": "object",
        "properties": {"actions": actions_schema},
        "required": ["actions"],
        "additionalProperties": False,
    }


def parse_decision(
    text: str,
    prepared: PreparedRequest,
) -> list[DecisionAction]:
    """Parse Codex's structured decision, falling back to plain text."""
    payload = _extract_json(text)
    if payload is None:
        return [DecisionAction(kind="message", text=text)]
    if isinstance(payload, str):
        return [DecisionAction(kind="message", text=payload)]

    raw_actions: Any
    if isinstance(payload, list):
        raw_actions = payload
    elif isinstance(payload, dict) and isinstance(payload.get("actions"), list):
        raw_actions = payload["actions"]
    elif isinstance(payload, dict) and "type" in payload:
        raw_actions = [payload]
    elif isinstance(payload, dict) and isinstance(payload.get("message"), str):
        raw_actions = [{"type": "message", "text": payload["message"]}]
    else:
        return [DecisionAction(kind="message", text=text)]

    tool_names = {tool.name for tool in prepared.tools}
    actions: list[DecisionAction] = []
    tool_calls = 0
    for index, raw_action in enumerate(raw_actions):
        if not isinstance(raw_action, dict):
            raise codex_tool_call_invalid(
                f"Codex returned a non-object action at index {index}."
            )
        action_type = raw_action.get("type", raw_action.get("kind"))
        if action_type == "message":
            message = raw_action.get("text", "")
            if message is None:
                message = ""
            if not isinstance(message, str):
                raise codex_tool_call_invalid(
                    f"Codex action {index} has a non-string message."
                )
            if message:
                actions.append(DecisionAction(kind="message", text=message))
            continue

        if action_type != "tool_call":
            raise codex_tool_call_invalid(
                f"Codex returned unknown action type {action_type!r} at index {index}."
            )

        name = raw_action.get("name")
        if not isinstance(name, str) or not name:
            raise codex_tool_call_invalid(
                f"Codex tool call {index} has no tool name."
            )
        if name not in tool_names:
            raise codex_tool_call_invalid(
                f"Codex requested unknown tool {name!r}. "
                f"Configured tools: {', '.join(sorted(tool_names)) or '(none)'}."
            )
        arguments = _normalize_arguments(raw_action.get("arguments"), index=index)
        if not prepared.parallel_tool_calls and tool_calls >= 1:
            logger.warning(
                "Dropping extra parallel tool call %s because parallel_tool_calls=false",
                name,
            )
            continue
        tool_calls += 1
        actions.append(
            DecisionAction(
                kind="tool_call",
                name=name,
                arguments=json.dumps(
                    arguments,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        )

    if not actions:
        raise codex_protocol_error("Codex returned no usable actions.")
    if prepared.tool_choice == "required" and tool_calls == 0:
        raise codex_protocol_error(
            "Codex returned a message although tool_choice required a tool call."
        )
    return actions


def _tool_protocol(prepared: PreparedRequest) -> str:
    tool_blocks: list[str] = []
    for tool in prepared.tools:
        parameters = json.dumps(tool.parameters, ensure_ascii=False, indent=2)
        tool_blocks.append(
            f"### {tool.name}\n"
            f"{tool.description or '(no description)'}\n"
            f"Parameters:\n```json\n{parameters}\n```"
        )
    tools_text = "\n\n".join(tool_blocks)
    required = (
        "You must request at least one tool call."
        if prepared.tool_choice == "required"
        else "Return a message when no tool is needed."
    )
    parallel = (
        "You may request multiple tool calls in one response when they are independent."
        if prepared.parallel_tool_calls
        else "Request at most one tool call."
    )
    return f"""<tool_protocol>
You are the model inside an external agent harness. The harness executes the tools below.
You cannot execute these tools yourself. Do not run your own shell, file, web, MCP, or patch tools
for this turn; only decide what the harness should do next. The harness will execute your tool
calls and send the results back in a later turn.

Available tools:

{tools_text}

Return exactly one JSON object and no surrounding prose. Every action must include all four
keys: `type`, `text`, `name`, and `arguments`. Use empty strings and `{{}}` for unused fields.
For a normal message:
{{"actions":[{{"type":"message","text":"...","name":"","arguments":"{{}}"}}]}}
For a tool call, set `arguments` to a JSON-encoded object string, for example
`{{\"command\":\"pwd\"}}` encoded as a string.

Rules:
- Use only the tool names listed above.
- `arguments` must encode a JSON object matching that tool's parameters.
- {required}
- {parallel}
- Do not wrap the JSON in markdown fences.
</tool_protocol>"""


def _extract_json(text: str) -> Any | None:
    candidate = text.strip()
    if not candidate:
        return None
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    starts = [position for position in (candidate.find("{"), candidate.find("[")) if position >= 0]
    if not starts:
        return None
    start = min(starts)
    try:
        payload, _ = json.JSONDecoder().raw_decode(candidate[start:])
    except json.JSONDecodeError:
        return None
    return payload


def _normalize_arguments(value: Any, *, index: int) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        arguments = value
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise codex_tool_call_invalid(
                f"Codex tool call {index} returned invalid JSON arguments: {value!r}"
            ) from exc
        if not isinstance(decoded, dict):
            raise codex_tool_call_invalid(
                f"Codex tool call {index} arguments must decode to an object."
            )
        arguments = decoded
    else:
        raise codex_tool_call_invalid(
            f"Codex tool call {index} arguments must be an object or JSON string."
        )
    try:
        json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise codex_tool_call_invalid(
            f"Codex tool call {index} arguments are not JSON-serializable."
        ) from exc
    return arguments
