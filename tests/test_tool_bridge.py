from __future__ import annotations

import pytest
from conftest import make_settings

from codex_responses_adapter.errors import AdapterError
from codex_responses_adapter.schemas import ResponsesRequest
from codex_responses_adapter.tool_bridge import (
    build_tool_prompt,
    decision_schema,
    parse_decision,
)


def prepared(*, parallel: bool = True, required: bool = False):
    return ResponsesRequest(
        input="Run pwd.",
        tools=[
            {
                "type": "function",
                "name": "bash",
                "description": "Run a shell command.",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
            {
                "type": "function",
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        ],
        tool_choice="required" if required else "auto",
        parallel_tool_calls=parallel,
    ).prepare(make_settings())


def test_tool_prompt_contains_tools_and_rules() -> None:
    prompt = build_tool_prompt(prepared())
    assert "<tool_protocol>" in prompt
    assert "### bash" in prompt
    assert "### read_file" in prompt
    assert "You cannot execute these tools yourself" in prompt
    assert "Run pwd." in prompt


def test_decision_schema_has_tool_enum_and_parallel_limit() -> None:
    schema = decision_schema(prepared(parallel=False))
    action = schema["properties"]["actions"]["items"]
    assert action["properties"]["name"]["enum"] == ["bash", "read_file"]
    assert schema["properties"]["actions"]["maxItems"] == 1


def test_parse_message_and_tool_call() -> None:
    actions = parse_decision(
        '{"actions":[{"type":"message","text":"Need a shell."},'
        '{"type":"tool_call","name":"bash","arguments":{"command":"pwd"}}]}',
        prepared(),
    )
    assert [(action.kind, action.name) for action in actions] == [
        ("message", ""),
        ("tool_call", "bash"),
    ]
    assert actions[1].arguments == '{"command":"pwd"}'


def test_parse_tool_call_with_string_arguments() -> None:
    actions = parse_decision(
        '{"actions":[{"type":"tool_call","name":"bash","arguments":"{\\"command\\":\\"ls\\"}"}]}',
        prepared(),
    )
    assert actions[0].arguments == '{"command":"ls"}'


def test_parse_plain_text_falls_back_to_message() -> None:
    actions = parse_decision("No tool is needed.", prepared())
    assert len(actions) == 1
    assert actions[0].kind == "message"
    assert actions[0].text == "No tool is needed."


def test_parse_markdown_fenced_json() -> None:
    actions = parse_decision(
        '```json\n{"actions":[{"type":"message","text":"ok"}]}\n```',
        prepared(),
    )
    assert actions[0].text == "ok"


def test_unknown_tool_is_rejected() -> None:
    with pytest.raises(AdapterError) as exc_info:
        parse_decision(
            '{"actions":[{"type":"tool_call","name":"nope","arguments":{}}]}',
            prepared(),
        )
    assert exc_info.value.code == "codex_tool_call_invalid"


def test_required_tool_choice_rejects_message_only() -> None:
    with pytest.raises(AdapterError) as exc_info:
        parse_decision(
            '{"actions":[{"type":"message","text":"I will not call a tool."}]}',
            prepared(required=True),
        )
    assert exc_info.value.code == "codex_protocol_error"


def test_parallel_limit_drops_extra_tool_calls() -> None:
    actions = parse_decision(
        '{"actions":['
        '{"type":"tool_call","name":"bash","arguments":{"command":"pwd"}},'
        '{"type":"tool_call","name":"bash","arguments":{"command":"ls"}}'
        "]}",
        prepared(parallel=False),
    )
    assert len(actions) == 1
    assert actions[0].arguments == '{"command":"pwd"}'
