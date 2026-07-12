"""Tests for strict context-window lookups in model metadata storage."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.storage.model_costs import ModelCostStore


def test_exact_positive_context_window_wins_without_prefix_lookup() -> None:
    db = MagicMock()
    db.fetchone.return_value = {"context_length": 200_000}

    result = ModelCostStore(db).get_context_window("gpt-valid")

    assert result == 200_000
    assert db.fetchone.call_count == 1
    assert "context_length > 0" in db.fetchone.call_args.args[0]


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
