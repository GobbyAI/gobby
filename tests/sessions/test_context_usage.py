from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.sessions.context_usage import (
    context_window_for_source_model,
    context_window_from_raw_message,
    effective_context_window_for_session,
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


def test_effective_context_window_repairs_stale_codex_static_value() -> None:
    session = SimpleNamespace(
        id="session-1",
        source="codex",
        model="gpt-5.4",
        context_window=200_000,
    )

    assert effective_context_window_for_session(session) == 258_400


def test_effective_context_window_preserves_reported_session_value() -> None:
    session = SimpleNamespace(
        id="session-1",
        source="codex",
        model="gpt-5.4",
        context_window=200_000,
        context_usage_confidence="reported",
    )

    assert effective_context_window_for_session(session) == 200_000


def test_effective_context_window_prefers_latest_token_event_window() -> None:
    class FakeDb:
        def fetchall(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return [
                {
                    "id": 1,
                    "session_id": "session-1",
                    "project_id": "proj-1",
                    "message_id": "msg-1",
                    "source": "codex",
                    "origin": "transcript",
                    "model": "gpt-5.4",
                    "model_family": "gpt-5.4",
                    "input_tokens": 100,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                    "context_window": 258_400,
                    "event_at": "2026-05-27T21:50:28Z",
                    "created_at": "2026-05-27T21:50:29Z",
                    "metadata": None,
                }
            ]

    session = SimpleNamespace(
        id="session-1",
        source="codex",
        model="gpt-5.4",
        context_window=200_000,
    )

    assert effective_context_window_for_session(session, db=FakeDb()) == 258_400


def test_context_window_from_raw_message_rejects_fractional_windows() -> None:
    assert context_window_from_raw_message({"context_window": 1.5}) is None
    assert context_window_from_raw_message({"context_window": 2.0}) == 2
