"""Tests for strict context-window lookups in model metadata storage."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.storage.model_costs import ModelCostStore


def test_populate_keeps_same_model_suffix_for_different_providers() -> None:
    from gobby.llm.model_registry import ModelInfo

    db = MagicMock()
    connection = db.transaction.return_value.__enter__.return_value
    models = [
        ModelInfo(
            id="anthropic/shared-model",
            name="Shared Claude",
            provider="claude",
            context_length=200_000,
            max_completion_tokens=8_000,
        ),
        ModelInfo(
            id="openai/shared-model",
            name="Shared Codex",
            provider="codex",
            context_length=128_000,
            max_completion_tokens=4_000,
        ),
    ]

    assert ModelCostStore(db).populate(models) == 2

    rows = connection.executemany.call_args.args[1]
    assert rows == [
        ("shared-model", "claude", 200_000, 8_000, "registry"),
        ("shared-model", "codex", 128_000, 4_000, "registry"),
    ]


def test_exact_positive_context_window_wins_without_prefix_lookup() -> None:
    db = MagicMock()
    db.fetchone.return_value = {"context_length": 200_000}

    result = ModelCostStore(db).get_context_window("gpt-valid")

    assert result == 200_000
    assert db.fetchone.call_count == 1
    assert "context_length > 0" in db.fetchone.call_args.args[0]


def test_provider_prefixed_lookup_is_provider_scoped() -> None:
    db = MagicMock()
    db.fetchone.return_value = {"context_length": 200_000}

    result = ModelCostStore(db).get_context_window("claude/shared-model")

    assert result == 200_000
    query, params = db.fetchone.call_args.args
    assert "provider = %s" in query
    assert params == ("claude", "shared-model")


def test_provider_prefixed_prefix_lookup_is_provider_scoped() -> None:
    db = MagicMock()
    db.fetchone.side_effect = [None, {"context_length": 200_000}]

    result = ModelCostStore(db).get_context_window("claude/shared-model-versioned")

    assert result == 200_000
    query, params = db.fetchone.call_args.args
    assert "provider = %s" in query
    assert "LEFT(%s, LENGTH(model)) = model" in query
    assert params == ("claude", "shared-model-versioned")


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

    result = ModelCostStore(db).get_context_window("gpt-family-versioned")

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

    assert ModelCostStore(db).get_context_window("gpt-family-versioned") is None
