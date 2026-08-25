from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.storage.context_usage_snapshot import ContextUsageSnapshot

pytestmark = pytest.mark.unit


def test_ratio_is_null_when_window_or_usage_is_unknown() -> None:
    assert ContextUsageSnapshot.calculate_ratio(None, 200_000) is None
    assert ContextUsageSnapshot.calculate_ratio(1_000, None) is None
    assert ContextUsageSnapshot.calculate_ratio(1_000, 0) is None


def test_ratio_is_clamped_to_valid_pressure_range() -> None:
    assert ContextUsageSnapshot.calculate_ratio(-10, 100) == 0.0
    assert ContextUsageSnapshot.calculate_ratio(50, 100) == 0.5
    assert ContextUsageSnapshot.calculate_ratio(150, 100) == 1.0


def test_token_breakdown_counts_cache_toward_prompt_footprint() -> None:
    snapshot = ContextUsageSnapshot.from_token_breakdown(
        source="codex",
        context_window=100_000,
        uncached_prompt_tokens=2_000,
        cache_read_tokens=30_000,
        cache_creation_tokens=3_000,
        output_tokens=500,
        model="gpt-5.3-codex",
    )

    assert snapshot.context_used_tokens == 35_000
    assert snapshot.raw_prompt_footprint == 35_000
    assert snapshot.uncached_prompt_tokens == 2_000
    assert snapshot.cache_read_tokens == 30_000
    assert snapshot.cache_creation_tokens == 3_000
    assert snapshot.output_tokens == 500
    assert snapshot.context_usage_ratio == 0.35
    assert snapshot.confidence == "reported"
    assert snapshot.source == "codex"
    assert snapshot.model == "gpt-5.3-codex"
    assert isinstance(snapshot.timestamp, datetime)
    assert snapshot.timestamp.tzinfo is UTC


def test_window_only_snapshot_keeps_pressure_unknown() -> None:
    snapshot = ContextUsageSnapshot.window_only(
        source="grok",
        context_window=512_000,
        model="grok-build",
    )

    assert snapshot.context_window == 512_000
    assert snapshot.context_used_tokens is None
    assert snapshot.context_usage_ratio is None
    assert snapshot.confidence == "unknown"


def test_reported_occupancy_stays_separate_from_token_breakdown() -> None:
    snapshot = ContextUsageSnapshot.from_reported_occupancy(
        source="codex",
        context_window=258_400,
        context_used_tokens=7_248,
        model="gpt-5.6-sol",
    )

    assert snapshot.context_used_tokens == 7_248
    assert snapshot.context_usage_ratio == pytest.approx(7_248 / 258_400)
    assert snapshot.raw_prompt_footprint == 7_248
    assert snapshot.uncached_prompt_tokens is None
    assert snapshot.cache_read_tokens is None
    assert snapshot.cache_creation_tokens is None
    assert snapshot.output_tokens is None
    assert snapshot.confidence == "reported"


def test_token_breakdown_ignores_bool_token_values() -> None:
    snapshot = ContextUsageSnapshot.from_token_breakdown(
        source="web_chat",
        context_window=200_000,
        uncached_prompt_tokens=True,
        cache_read_tokens=False,
        cache_creation_tokens=True,
        output_tokens=False,
    )

    assert snapshot.context_used_tokens is None
    assert snapshot.raw_prompt_footprint is None
    assert snapshot.uncached_prompt_tokens is None
    assert snapshot.cache_read_tokens is None
    assert snapshot.cache_creation_tokens is None
    assert snapshot.output_tokens is None
    assert snapshot.confidence == "unknown"


def test_token_breakdown_ignores_malformed_token_values() -> None:
    snapshot = ContextUsageSnapshot.from_token_breakdown(
        source="web_chat",
        context_window=200_000,
        uncached_prompt_tokens="not-a-number",  # type: ignore[arg-type]
        cache_read_tokens=object(),  # type: ignore[arg-type]
        cache_creation_tokens="5",
        output_tokens=-3,
    )

    assert snapshot.uncached_prompt_tokens is None
    assert snapshot.cache_read_tokens is None
    assert snapshot.cache_creation_tokens == 5
    assert snapshot.output_tokens == 0
