from __future__ import annotations

from codex_responses_adapter.prompt_budget import fit_prompt


def test_small_prompt_is_unchanged() -> None:
    prompt = "hello"
    assert fit_prompt(prompt, 10_000) == prompt


def test_large_prompt_keeps_head_and_tail() -> None:
    prompt = "HEAD\n" * 3_000 + "old-middle\n" * 3_000 + "TAIL\n" * 3_000
    fitted = fit_prompt(prompt, 10_000)
    assert len(fitted) <= 10_000
    assert fitted.startswith("HEAD")
    assert fitted.endswith("TAIL\n")
    assert "characters of older conversation omitted" in fitted


def test_fit_prompt_uses_tail_space_after_marker() -> None:
    prompt = "A" * 20_000 + "B" * 20_000
    fitted = fit_prompt(prompt, 10_000)
    assert len(fitted) <= 10_000
    assert fitted.endswith("B" * 100)
