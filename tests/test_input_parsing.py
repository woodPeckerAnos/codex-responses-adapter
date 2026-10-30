from __future__ import annotations

import pytest
from conftest import make_settings

from codex_responses_adapter.errors import AdapterError
from codex_responses_adapter.schemas import ResponsesRequest


def test_string_input_passes_through() -> None:
    prepared = ResponsesRequest(input="Hello").prepare(make_settings())
    assert prepared.prompt == "Hello"
    assert prepared.model == "codex-cli"
    assert prepared.store is True
    assert prepared.temperature == 1.0
    assert prepared.top_p == 1.0


def test_single_user_message_is_not_wrapped() -> None:
    prepared = ResponsesRequest(
        input=[{"role": "user", "content": "Hello"}],
    ).prepare(make_settings())
    assert prepared.prompt == "Hello"


def test_message_parts_are_joined() -> None:
    prepared = ResponsesRequest(
        input=[
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "first"},
                    {"type": "input_text", "text": "second"},
                ],
            }
        ],
    ).prepare(make_settings())
    assert prepared.prompt == "first\nsecond"


def test_roles_are_tagged_and_instructions_prepended() -> None:
    prepared = ResponsesRequest(
        instructions="Be concise.",
        input=[
            {"role": "system", "content": "You are a reviewer."},
            {"role": "user", "content": "Review this."},
            {"role": "assistant", "content": "Previous answer."},
        ],
    ).prepare(make_settings())
    assert prepared.instructions == "Be concise."
    assert prepared.prompt.startswith("<instructions>\nBe concise.\n</instructions>")
    assert "<system>\nYou are a reviewer.\n</system>" in prepared.prompt
    assert "<assistant>\nPrevious answer.\n</assistant>" in prepared.prompt
    assert "Review this." in prepared.prompt


def test_model_echo_and_store_override() -> None:
    prepared = ResponsesRequest(
        model="gpt-test",
        input="hi",
        store=False,
        previous_response_id="resp_123",
        metadata={"tenant": "test"},
        temperature=0.2,
        top_p=0.8,
        max_output_tokens=50,
        user="user-1",
    ).prepare(make_settings())
    assert prepared.model == "gpt-test"
    assert prepared.store is False
    assert prepared.previous_response_id == "resp_123"
    assert prepared.metadata == {"tenant": "test"}
    assert prepared.temperature == 0.2
    assert prepared.top_p == 0.8
    assert prepared.max_output_tokens == 50
    assert prepared.user == "user-1"


def test_blank_previous_response_id_is_none() -> None:
    prepared = ResponsesRequest(input="hi", previous_response_id="  ").prepare(make_settings())
    assert prepared.previous_response_id is None


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (
            {"input": [{"role": "user", "content": [{"type": "input_image"}]}]},
            "unsupported_parameter",
        ),
        (
            {"input": [{"role": "user", "content": [{"type": "input_file"}]}]},
            "unsupported_parameter",
        ),
        (
            {"input": "hi", "tools": [{"type": "function", "name": "x"}]},
            "unsupported_parameter",
        ),
        (
            {"input": "hi", "text": {"format": {"type": "json_schema"}}},
            "unsupported_parameter",
        ),
        (
            {"input": "hi", "background": True},
            "unsupported_parameter",
        ),
        (
            {"input": "hi", "conversation": "conv_123"},
            "unsupported_parameter",
        ),
    ],
)
def test_unsupported_features_are_rejected(payload: dict[str, object], code: str) -> None:
    with pytest.raises(AdapterError) as exc_info:
        ResponsesRequest(**payload).prepare(make_settings())
    assert exc_info.value.code == code


@pytest.mark.parametrize(
    "payload",
    [
        {"input": ""},
        {"input": []},
        {"input": [{"role": "user", "content": []}]},
        {"input": [{"role": "tool", "content": "x"}]},
        {"input": [{"type": "function_call", "role": "user", "content": "x"}]},
    ],
)
def test_invalid_text_inputs_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(AdapterError) as exc_info:
        ResponsesRequest(**payload).prepare(make_settings())
    assert exc_info.value.status_code == 400
