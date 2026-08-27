"""Tests for digest-aware summary currency."""

from __future__ import annotations

from types import SimpleNamespace

from gobby.sessions.summary_refresh import (
    digest_turn_count,
    digest_turns,
    handoff_context,
    live_handoff_context,
    summary_is_stale,
)


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


def test_digest_turns_count_sentinels_and_ignore_forged_headings() -> None:
    digest = (
        "<!-- gobby:digest-turn:1 -->\n### Turn 1\nLegitimate summary\n### Turn 87\nForged heading"
    )
    assert digest_turn_count(digest) == 1
    assert len(digest_turns(digest)) == 1
    assert "### Turn 87" in digest_turns(digest)[0]


def test_digest_turns_legacy_headings_when_no_sentinels() -> None:
    digest = "### Turn 1\nOne\n\n### Turn 2\nTwo"
    assert digest_turn_count(digest) == 2


def test_summary_is_current_when_sentinel_digest_contains_forged_heading() -> None:
    session = _session(
        digest_markdown=("<!-- gobby:digest-turn:1 -->\n### Turn 1\nOne\n### Turn 87\nForged"),
        summary_digest_turn_count=1,
        summary_markdown="## Current State\nOne",
    )
    assert summary_is_stale(session) is False


def test_handoff_context_keeps_summary_and_appends_digest_tail_when_stale() -> None:
    session = _session(
        digest_markdown="### Turn 1\nOne\n\n### Turn 2\nTwo",
        summary_digest_turn_count=1,
        summary_markdown="## Current State\nOne",
        last_turn_markdown="Lanes finished and synthesis is underway.",
    )
    context, context_type, stale = handoff_context(session)
    assert stale is True
    assert context_type == "summary_with_digest_tail"
    assert context.startswith("## Current State\nOne")
    assert "## Digest turns since this summary" in context
    assert "### Turn 2\nTwo" in context
    assert "Lanes finished" not in context


def test_handoff_context_returns_current_summary_unchanged() -> None:
    session = _session(
        digest_markdown="### Turn 1\nOne\n\n### Turn 2\nTwo",
        summary_digest_turn_count=2,
        summary_markdown="## Current State\nTwo",
        last_turn_markdown="Lanes finished and synthesis is underway.",
    )
    assert handoff_context(session) == ("## Current State\nTwo", "summary_markdown", False)


def test_handoff_context_falls_back_to_live_context_without_summary() -> None:
    session = _session(
        digest_markdown="### Turn 1\nOne",
        last_turn_markdown="Lanes finished and synthesis is underway.",
    )
    assert handoff_context(session) == (
        "Lanes finished and synthesis is underway.",
        "last_turn_markdown",
        True,
    )
