"""Tests for canonical Codex plan-handoff title extraction."""

from __future__ import annotations

import pytest

from gobby.hooks.handoff_titles import (
    CODEX_PLAN_HANDOFF_PREFIX,
    extract_codex_handoff_title,
)

pytestmark = pytest.mark.unit


def test_extracts_canonical_handoff_h1() -> None:
    prompt = f"{CODEX_PLAN_HANDOFF_PREFIX}\n\n# Seed Codex Handoff Titles\n\n## Summary"

    assert extract_codex_handoff_title(prompt) == "Seed Codex Handoff Titles"


def test_accepts_leading_whitespace() -> None:
    prompt = f"\n  {CODEX_PLAN_HANDOFF_PREFIX}\n\n   # Preserve Heading Text"

    assert extract_codex_handoff_title(prompt) == "Preserve Heading Text"


def test_accepts_crlf_input() -> None:
    prompt = f"{CODEX_PLAN_HANDOFF_PREFIX}\r\n\r\n# CRLF Handoff\r\n"

    assert extract_codex_handoff_title(prompt) == "CRLF Handoff"


def test_preserves_plan_prefix_in_heading() -> None:
    prompt = f"{CODEX_PLAN_HANDOFF_PREFIX}\n\n# Plan: Keep Exact Heading"

    assert extract_codex_handoff_title(prompt) == "Plan: Keep Exact Heading"


def test_truncates_long_handoff_h1_with_existing_title_validator() -> None:
    prompt = f"{CODEX_PLAN_HANDOFF_PREFIX}\n\n# {' '.join(['word'] * 30)}"

    assert extract_codex_handoff_title(prompt) == " ".join(["word"] * 16)


@pytest.mark.parametrize(
    "prompt",
    [
        (
            "A previous agent produced the plan below to accomplish the user's task. "
            "Implement the plan in a fresh context.\n\n# Incomplete Prefix"
        ),
        f"{CODEX_PLAN_HANDOFF_PREFIX}\n\nSummary without a heading",
        f"{CODEX_PLAN_HANDOFF_PREFIX}\n\n## H2 Only",
        "Please implement this ordinary prompt.\n\n# Incidental Heading",
    ],
)
def test_rejects_noncanonical_or_headingless_prompts(prompt: str) -> None:
    assert extract_codex_handoff_title(prompt) is None
