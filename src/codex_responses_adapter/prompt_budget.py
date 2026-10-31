"""Keep Codex prompts below the CLI's hard input-character limit.

Codex's app-server rejects turns whose serialized input exceeds 1,048,576
characters. Model context-window limits are separate: a conversation can fit
the model while still exceeding this transport cap. Truncating the oldest
middle section keeps the current request and recent tool results usable.
"""

from __future__ import annotations

import logging

from .errors import invalid_request

logger = logging.getLogger(__name__)

CODEX_INPUT_CHAR_LIMIT = 1_048_576
DEFAULT_MAX_PROMPT_CHARS = 1_000_000
_MIN_PROMPT_CHARS = 10_000
_MAX_HEAD_CHARS = 200_000


def fit_prompt(prompt: str, max_chars: int) -> str:
    """Return ``prompt`` at or below ``max_chars``, dropping oldest middle text.

    The beginning is preserved for instructions/tool protocol and the end is
    preserved for the latest user turn and tool results. A visible marker tells
    the model that older context was omitted.
    """
    if len(prompt) <= max_chars:
        return prompt
    if max_chars < _MIN_PROMPT_CHARS:
        raise invalid_request(
            f"CRA_MAX_PROMPT_CHARS must be at least {_MIN_PROMPT_CHARS}.",
            param="max_prompt_chars",
        )

    head_budget = min(_MAX_HEAD_CHARS, max_chars // 4)
    tail_budget = max_chars - head_budget - 256
    if tail_budget <= 0:
        raise invalid_request(
            "CRA_MAX_PROMPT_CHARS is too small to retain prompt context.",
            param="max_prompt_chars",
        )

    head = prompt[:head_budget]
    newline = head.rfind("\n")
    if newline > head_budget // 2:
        head = head[: newline + 1]

    tail = prompt[-tail_budget:]
    newline = tail.find("\n")
    if 0 <= newline < tail_budget // 2:
        tail = tail[newline + 1 :]

    omitted = len(prompt) - len(head) - len(tail)
    marker = (
        "\n\n[... "
        f"{omitted} characters of older conversation omitted by the adapter "
        "to fit Codex's input limit ...]"
        "\n\n"
    )
    result = head + marker + tail
    if len(result) > max_chars:
        overflow = len(result) - max_chars
        result = head + marker + tail[overflow:]
    if len(result) > max_chars:
        result = result[-max_chars:]

    logger.warning(
        "Prompt truncated from %d to %d characters (limit %d, omitted %d)",
        len(prompt),
        len(result),
        max_chars,
        omitted,
    )
    return result
