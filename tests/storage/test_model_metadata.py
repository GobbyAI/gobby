"""Tests for strict context-window lookups in model metadata storage."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from gobby.storage import model_metadata
from gobby.storage.model_metadata import ModelMetadataStore


def test_populate_dedupes_shared_model_ids_keeping_larger_context_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.llm.model_registry import ModelInfo

    monkeypatch.setattr(model_metadata, "_stale_warning_emitted", True)
    db = MagicMock()
    connection = db.transaction.return_value.__enter__.return_value
    models = [
        ModelInfo(
            id="anthropic/shared-model",
            name="Shared Claude",
            context_length=200_000,
            max_completion_tokens=8_000,
        ),
        ModelInfo(
            id="shared-model",
            name="Shared (smaller tier)",
            context_length=128_000,
            max_completion_tokens=4_000,
        ),
    ]

    assert ModelMetadataStore(db).populate(models) == 1

    rows = connection.executemany.call_args.args[1]
    assert rows == [("shared-model", 200_000, 8_000, "registry")]
    assert model_metadata._stale_warning_emitted is False


def test_empty_populate_retains_cached_metadata() -> None:
    db = MagicMock()

    assert ModelMetadataStore(db).populate([]) == 0

    db.transaction.assert_not_called()


def test_stale_metadata_warns_once(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_metadata, "_stale_warning_emitted", False)
    db = MagicMock()
    db.fetchone.return_value = {
        "context_length": 200_000,
        "metadata_updated_at": datetime.now(UTC) - timedelta(hours=49),
    }
    store = ModelMetadataStore(db)

    with caplog.at_level(logging.WARNING, logger="gobby.storage.model_metadata"):
        assert store.get_context_window("gpt-valid") == 200_000
        assert store.get_context_window("gpt-valid") == 200_000

    warnings = [record for record in caplog.records if "older than 48 hours" in record.message]
    assert len(warnings) == 1


def test_exact_positive_context_window_wins_without_prefix_lookup() -> None:
    db = MagicMock()
    db.fetchone.return_value = {"context_length": 200_000}

    result = ModelMetadataStore(db).get_context_window("gpt-valid")

    assert result == 200_000
    assert db.fetchone.call_count == 1
    assert "context_length > 0" in db.fetchone.call_args.args[0]


def test_vendor_prefixed_lookup_normalizes_to_bare_model() -> None:
    db = MagicMock()
    db.fetchone.return_value = {"context_length": 200_000}

    result = ModelMetadataStore(db).get_context_window("anthropic/shared-model")

    assert result == 200_000
    query, params = db.fetchone.call_args.args
    assert "provider" not in query
    assert "WHERE model = %s" in query
    assert params == ("shared-model",)


def test_endpoint_prefixed_lookup_normalizes_to_bare_model() -> None:
    db = MagicMock()
    db.fetchone.return_value = {"context_length": 200_000}

    result = ModelMetadataStore(db).get_context_window("endpoint:fast/openai/gpt-5.4")

    assert result == 200_000
    _query, params = db.fetchone.call_args.args
    assert params == ("gpt-5.4",)


def test_prefix_lookup_matches_longest_bare_model_key() -> None:
    db = MagicMock()
    db.fetchone.side_effect = [None, {"context_length": 200_000}]

    result = ModelMetadataStore(db).get_context_window("anthropic/shared-model-versioned")

    assert result == 200_000
    query, params = db.fetchone.call_args.args
    assert "provider" not in query
    assert "LEFT(%s, LENGTH(model)) = model" in query
    assert "ORDER BY LENGTH(model) DESC" in query
    assert params == ("shared-model-versioned",)


@pytest.mark.parametrize(
    "invalid_value",
    [
        pytest.param(None, id="null"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(True, id="bool"),
        pytest.param("malformed", id="string"),
        pytest.param(1.5, id="float"),
    ],
)
def test_invalid_exact_row_does_not_shadow_positive_prefix(invalid_value: object) -> None:
    db = MagicMock()
    db.fetchone.side_effect = [
        {"context_length": invalid_value},
        {"context_length": 128_000},
    ]

    result = ModelMetadataStore(db).get_context_window("gpt-family-versioned")

    assert result == 128_000
    assert db.fetchone.call_count == 2
    exact_query = db.fetchone.call_args_list[0].args[0]
    prefix_query = db.fetchone.call_args_list[1].args[0]
    assert "context_length > 0" in exact_query
    assert "context_length > 0" in prefix_query
    assert "LIKE" not in prefix_query
    assert "LEFT(%s, LENGTH(model)) = model" in prefix_query


@pytest.mark.parametrize(
    "invalid_value",
    [
        pytest.param(None, id="null"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(True, id="bool"),
        pytest.param("malformed", id="string"),
        pytest.param(1.5, id="float"),
    ],
)
def test_invalid_prefix_row_is_rejected(invalid_value: object) -> None:
    db = MagicMock()
    db.fetchone.side_effect = [None, {"context_length": invalid_value}]

    assert ModelMetadataStore(db).get_context_window("gpt-family-versioned") is None
