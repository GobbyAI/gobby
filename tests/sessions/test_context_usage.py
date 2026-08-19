from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gobby.sessions import context_usage
from gobby.sessions.context_usage import (
    backfill_session_context_windows,
    context_window_for_source_model,
    context_window_from_raw_message,
    effective_context_window_for_session,
    snapshot_from_token_usage,
    snapshot_from_window_metadata,
)
from gobby.sessions.transcripts.base import TokenUsage

pytestmark = pytest.mark.unit


class _TransactionFakeDb:
    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield


def test_snapshot_builders_resolve_one_million_context_marker() -> None:
    token_snapshot = snapshot_from_token_usage(
        source="claude",
        context_window=200_000,
        usage=TokenUsage(input_tokens=125_071, output_tokens=1),
        model="claude-opus-4-8[1m]",
    )
    window_snapshot = snapshot_from_window_metadata(
        source="claude",
        context_window=200_000,
        model="claude-sonnet-4-6[1m]",
    )

    assert token_snapshot is not None
    assert token_snapshot.context_window == 1_000_000
    assert token_snapshot.context_usage_ratio == pytest.approx(0.125071)
    assert window_snapshot is not None
    assert window_snapshot.context_window == 1_000_000


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gemini-3.5-flash", 1_048_576),
        ("agy/gemini-3.5-flash", 1_048_576),
        ("Gemini 3.5 Flash (Medium)", 1_048_576),
        ("gemini-3.1-pro", 1_000_000),
        ("Gemini 3.1 Pro (High)", 1_000_000),
        ("claude-sonnet-4-6", 1_000_000),
        ("Claude Sonnet 4.6 (Thinking)", 1_000_000),
        ("claude-opus-4-6", 1_000_000),
        ("Claude Opus 4.6 (Thinking)", 1_000_000),
        ("gpt-oss-120b", 131_072),
        ("GPT-OSS 120B (Medium)", 131_072),
    ],
)
def test_agy_uses_model_family_context_windows(model: str, expected: int) -> None:
    registry_value = None if "gpt-oss" in model.lower() else expected
    with patch("gobby.llm.model_registry.lookup_context_window", return_value=registry_value):
        assert context_window_for_source_model("agy", model) == expected


def test_agy_does_not_use_family_fallback_for_unknown_models() -> None:
    assert context_window_for_source_model("agy", "unknown-claudeish-model") is None


def test_agy_gemini_family_lookup_uses_agy_provider_catalog() -> None:
    with patch(
        "gobby.sessions.context_usage.resolve_context_window", return_value=1_048_576
    ) as resolve:
        assert context_usage._context_window_for_agy_model("gemini-3.5-flash") == 1_048_576

    resolve.assert_called_once_with(
        "gemini-3.5-flash",
        overrides=None,
        provider="agy",
        db=None,
    )


def test_private_resolver_uses_normalized_source_without_renormalizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_normalize(_source: str | None) -> None:
        raise AssertionError("private resolver should receive normalized source")

    def fake_resolve_context_window(
        model: str | None,
        *,
        provider: str | None,
        overrides: dict[str, int] | None = None,
        db: object | None = None,
    ) -> int:
        assert model == "model-x"
        assert provider == "claude"
        assert overrides is None
        assert db is None
        return 123

    monkeypatch.setattr(context_usage, "normalize_context_usage_source", fail_normalize)
    monkeypatch.setattr(context_usage, "resolve_context_window", fake_resolve_context_window)

    assert context_usage._resolve_context_window_for_source_model("claude", "model-x") == 123


def test_grok_window_only_snapshot_uses_model_metadata() -> None:
    with patch("gobby.llm.model_registry.lookup_context_window", return_value=512_000):
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
    with patch("gobby.llm.model_registry.lookup_context_window", return_value=1_000_000):
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


def test_effective_context_window_repairs_stale_codex_value_from_registry() -> None:
    session = SimpleNamespace(
        id="session-1",
        source="codex",
        model="gpt-5.4",
        context_window=200_000,
    )

    with patch("gobby.llm.model_registry.lookup_context_window", return_value=258_400):
        assert effective_context_window_for_session(session) == 258_400


def test_context_window_overrides_applied() -> None:
    session = SimpleNamespace(
        id="session-override",
        source="codex",
        model="future-model",
        context_window=None,
    )
    with patch("gobby.llm.model_registry.lookup_context_window", return_value=None):
        result = effective_context_window_for_session(
            session,
            overrides={"future-model": 333_000},
        )

    assert result == 333_000


def test_effective_context_window_preserves_reported_session_value() -> None:
    session = SimpleNamespace(
        id="session-1",
        source="codex",
        model="gpt-5.4",
        context_window=200_000,
        context_usage_confidence="reported",
    )

    assert effective_context_window_for_session(session) == 200_000


def test_effective_context_window_uses_reported_db_session_value() -> None:
    class FakeDb(_TransactionFakeDb):
        def fetchall(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return []

        def fetchone(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {"context_window": 200_000, "context_usage_confidence": "reported"}

    session = SimpleNamespace(
        id="session-1",
        source="unknown-provider",
        model="unknown-model",
    )

    assert effective_context_window_for_session(session, db=FakeDb()) == 200_000


def test_effective_context_window_prefers_reported_db_value_over_model_fallback() -> None:
    class FakeDb(_TransactionFakeDb):
        def fetchall(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return []

        def fetchone(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {"context_window": 175_000, "context_usage_confidence": "reported"}

    session = SimpleNamespace(
        id="session-1",
        source="codex",
        model="gpt-5.4",
    )

    assert effective_context_window_for_session(session, db=FakeDb()) == 175_000


def test_effective_context_window_ignores_non_reported_db_session_value() -> None:
    class FakeDb(_TransactionFakeDb):
        def fetchall(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return []

        def fetchone(self, sql: str, *_args: object, **_kwargs: object) -> dict[str, object] | None:
            # The capability resolver also queries config_store through this
            # db; only the sessions lookup should return the canned row.
            if "FROM sessions" not in sql:
                return None
            return {"context_window": 200_000, "context_usage_confidence": "inferred"}

    session = SimpleNamespace(
        id="session-1",
        source="unknown-provider",
        model="unknown-model",
    )

    with patch(
        "gobby.sessions.context_usage._resolve_context_window_for_source_model",
        return_value=None,
    ):
        assert effective_context_window_for_session(session, db=FakeDb()) is None


def test_effective_context_window_prefers_latest_token_event_window() -> None:
    class FakeDb(_TransactionFakeDb):
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


def test_context_window_from_raw_message_truncates_fractional_windows() -> None:
    assert context_window_from_raw_message({"context_window": 1.5}) == 1
    assert context_window_from_raw_message({"context_window": 2.0}) == 2


class _BackfillFakeDb:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.updates: list[tuple[object, ...]] = []

    def fetchall(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return self.rows

    def fetchone(self, *_args: object, **_kwargs: object) -> dict[str, object] | None:
        # config_store lookups from the capability resolver land here; no
        # aliases are configured in this fake.
        return None

    def transaction(self) -> _BackfillFakeDb:
        return self

    def __enter__(self) -> _BackfillFakeDb:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, _sql: str, params: tuple[object, ...] = ()) -> SimpleNamespace:
        self.updates.append(params)
        return SimpleNamespace(rowcount=1)


def test_backfill_bumps_under_counted_windows_and_recomputes_ratio() -> None:
    rows: list[dict[str, object]] = [
        # 1M-context Opus stored at the old 200k default -> bump to 1M, recompute.
        {
            "id": "s1",
            "model": "claude-opus-4-8",
            "source": "claude",
            "context_window": 200_000,
            "context_used_tokens": 418_834,
        },
        # [1m] marker, window never recorded (0) -> bump to 1M.
        {
            "id": "s2",
            "model": "claude-opus-4-8[1m]",
            "source": "claude",
            "context_window": 0,
            "context_used_tokens": 150_000,
        },
        # Already correct -> untouched.
        {
            "id": "s3",
            "model": "claude-opus-4-8",
            "source": "claude",
            "context_window": 1_000_000,
            "context_used_tokens": 500_000,
        },
        # Genuine larger window must never be shrunk to the family default.
        {
            "id": "s4",
            "model": "claude-sonnet-4-6",
            "source": "claude",
            "context_window": 1_000_000,
            "context_used_tokens": 10_000,
        },
        # Unknown model resolves to None -> skipped.
        {
            "id": "s5",
            "model": "totally-unknown-model",
            "source": "unknown",
            "context_window": 100,
            "context_used_tokens": 50,
        },
    ]
    db = _BackfillFakeDb(rows)

    def registry_window(model: str, **_kwargs: object) -> int | None:
        return 1_000_000 if "claude-opus-4-8" in model else None

    with patch(
        "gobby.sessions.context_usage._resolve_context_window_for_source_model",
        side_effect=lambda source, model, **_kwargs: registry_window(str(model)),
    ):
        result = backfill_session_context_windows(db)  # type: ignore[arg-type]

    assert result.scanned == 5
    assert result.updated == 2
    assert result.skipped == 3

    by_id = {params[-1]: params for params in db.updates}
    assert set(by_id) == {"s1", "s2"}
    # s1: window bumped to 1M, ratio = 418834 / 1_000_000.
    assert by_id["s1"][0] == 1_000_000
    assert by_id["s1"][1] == pytest.approx(0.418834)
    # s2: window bumped to 1M from an unrecorded (0) value.
    assert by_id["s2"][0] == 1_000_000
    assert by_id["s2"][1] == pytest.approx(0.15)


def test_backfill_dry_run_writes_nothing() -> None:
    rows: list[dict[str, object]] = [
        {
            "id": "s1",
            "model": "claude-opus-4-8",
            "source": "claude",
            "context_window": 200_000,
            "context_used_tokens": 418_834,
        }
    ]
    db = _BackfillFakeDb(rows)
    with patch(
        "gobby.sessions.context_usage._resolve_context_window_for_source_model",
        return_value=1_000_000,
    ):
        result = backfill_session_context_windows(db, dry_run=True)  # type: ignore[arg-type]

    assert result.updated == 1
    assert db.updates == []
