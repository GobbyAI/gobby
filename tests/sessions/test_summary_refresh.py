"""Tests for digest-aware summary currency."""

from __future__ import annotations

from types import SimpleNamespace

from gobby.sessions.summary_refresh import live_handoff_context, summary_is_stale


def _session(**kwargs: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "digest_markdown": None,
        "summary_digest_turn_count": None,
        "summary_markdown": None,
        "last_turn_markdown": None,
        "last_assistant_content": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_summary_is_current_when_watermark_matches_digest() -> None:
    session = _session(
        digest_markdown="### Turn 1\nOne\n\n### Turn 2\nTwo",
        summary_digest_turn_count=2,
        summary_markdown="## Current State\nTwo",
    )
    assert summary_is_stale(session) is False


def test_summary_is_stale_when_digest_grows_past_watermark() -> None:
    session = _session(
        digest_markdown="### Turn 1\nOne\n\n### Turn 2\nTwo",
        summary_digest_turn_count=1,
        summary_markdown="## Current State\nOne",
        last_turn_markdown="Lanes finished and synthesis is underway.",
    )
    assert summary_is_stale(session) is True


def test_summary_is_stale_when_digest_exists_without_watermark() -> None:
    session = _session(digest_markdown="### Turn 1\nOne")
    assert summary_is_stale(session) is True


def test_summary_without_digest_is_not_stale() -> None:
    session = _session(summary_markdown="## Summary\nReady")
    assert summary_is_stale(session) is False


def test_live_handoff_prefers_last_turn() -> None:
    session = _session(
        digest_markdown="### Turn 1\nOne\n\n### Turn 2\nTwo",
        summary_digest_turn_count=1,
        summary_markdown="## Current State\nOne",
        last_turn_markdown="Lanes finished and synthesis is underway.",
        last_assistant_content="older assistant text",
    )
    context, context_type = live_handoff_context(session)
    assert context_type == "last_turn_markdown"
    assert context == "Lanes finished and synthesis is underway."


def test_live_handoff_uses_digest_tail_without_last_turn() -> None:
    session = _session(
        digest_markdown="### Turn 1\nOne\n\n### Turn 2\nTwo",
        summary_digest_turn_count=1,
        summary_markdown="## Current State\nOne",
    )
    context, context_type = live_handoff_context(session)
    assert context_type == "digest_tail"
    assert "### Turn 2" in context
    assert "Two" in context
