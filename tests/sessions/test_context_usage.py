from __future__ import annotations

import pytest

from gobby.sessions.context_usage import (
    context_window_for_source_model,
    snapshot_from_window_metadata,
)

pytestmark = pytest.mark.unit


def test_agy_uses_gemini_family_context_window_fallback() -> None:
    assert context_window_for_source_model("agy", "gemini-2.5-pro") == 1_000_000


def test_grok_window_only_snapshot_uses_model_metadata() -> None:
    snapshot = snapshot_from_window_metadata(
        source="grok",
        context_window=None,
        model="grok-build",
    )

    assert snapshot is not None
    assert snapshot.source == "grok"
    assert snapshot.model == "grok-build"
    assert snapshot.context_window == 512_000
    assert snapshot.context_used_tokens is None
    assert snapshot.context_usage_ratio is None
    assert snapshot.confidence == "unknown"


def test_agy_window_only_snapshot_has_unknown_pressure() -> None:
    snapshot = snapshot_from_window_metadata(
        source="agy",
        context_window=None,
        model="gemini-2.5-pro",
    )

    assert snapshot is not None
    assert snapshot.source == "agy"
    assert snapshot.context_window == 1_000_000
    assert snapshot.context_used_tokens is None
    assert snapshot.context_usage_ratio is None
