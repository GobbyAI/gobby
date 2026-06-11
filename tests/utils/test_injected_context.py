"""Tests for injected context stripping helpers."""

from __future__ import annotations

import pytest

from gobby.utils.injected_context import (
    INJECTED_CONTEXT_BEGIN,
    INJECTED_CONTEXT_END,
    strip_injected_context,
)

pytestmark = pytest.mark.unit


def test_strip_balanced_sentinel_block() -> None:
    text = f"before\n{INJECTED_CONTEXT_BEGIN}\ninjected\n{INJECTED_CONTEXT_END}\nafter"

    assert strip_injected_context(text) == "before\nafter"


def test_strip_lone_begin_to_end_of_string() -> None:
    text = f"before\n{INJECTED_CONTEXT_BEGIN}\ninjected"

    assert strip_injected_context(text) == "before"


def test_strip_lone_end_from_start() -> None:
    text = f"injected\n{INJECTED_CONTEXT_END}\nafter"

    assert strip_injected_context(text) == "after"


def test_strip_multiple_sentinel_blocks() -> None:
    text = (
        f"one\n{INJECTED_CONTEXT_BEGIN}\na\n{INJECTED_CONTEXT_END}\n"
        f"two\n{INJECTED_CONTEXT_BEGIN}\nb\n{INJECTED_CONTEXT_END}\nthree"
    )

    assert strip_injected_context(text) == "one\ntwo\nthree"


def test_strip_legacy_human_marker_block() -> None:
    text = (
        "Keep this\n\n"
        "## Previous Session Context\n"
        "*Injected by Gobby session handoff*\n\n"
        "/Users/josh/Projects/gobby/src/gobby/memory/recall.py\n\n"
        "# Authored Section\n"
        "Keep this too"
    )

    assert strip_injected_context(text) == "Keep this\n\n# Authored Section\nKeep this too"


def test_strip_legacy_human_marker_block_stops_at_digest_turn_heading() -> None:
    text = (
        "### Turn 1\n"
        "*Injected by Gobby session handoff*\n\n"
        "/Users/josh/Projects/gobby/src/gobby/memory/recall.py\n\n"
        "### Turn 2\n"
        "Keep this real turn"
    )

    assert strip_injected_context(text) == "### Turn 1\n\n### Turn 2\nKeep this real turn"


def test_no_marker_returns_identity() -> None:
    text = "Plain authored text\nwith no injected context."

    assert strip_injected_context(text) is text


def test_full_empty_result() -> None:
    text = f"{INJECTED_CONTEXT_BEGIN}\ninjected only\n{INJECTED_CONTEXT_END}"

    assert strip_injected_context(text) == ""
