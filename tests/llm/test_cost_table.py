"""Tests for the DB-backed model cost table."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from gobby.llm.cost_table import ModelCost, _costs, calculate_cost, init, lookup_cost
from gobby.storage.model_costs import ModelCostStore

if TYPE_CHECKING:
    from gobby.storage.database import LocalDatabase

pytestmark = pytest.mark.unit


@pytest.fixture()
def populated_db(temp_db: LocalDatabase) -> LocalDatabase:
    """DB with model_costs populated from LiteLLM."""
    store = ModelCostStore(temp_db)
    count = store.populate_from_litellm()
    assert count > 0, "LiteLLM should have model cost data"
    init(temp_db)
    return temp_db


class TestModelCostStore:
    """Tests for the ModelCostStore storage layer."""

    def test_populate_returns_count(self, temp_db: LocalDatabase) -> None:
        """populate_from_litellm inserts models and returns count."""
        store = ModelCostStore(temp_db)
        count = store.populate_from_litellm()
        assert count > 100  # LiteLLM has hundreds of models

    def test_populate_is_idempotent(self, temp_db: LocalDatabase) -> None:
        """Calling populate twice replaces data, not appends."""
        store = ModelCostStore(temp_db)
        count1 = store.populate_from_litellm()
        count2 = store.populate_from_litellm()
        assert count1 == count2

    def test_get_all_returns_dict(self, temp_db: LocalDatabase) -> None:
        """get_all returns model -> (input, output) dict."""
        store = ModelCostStore(temp_db)
        store.populate_from_litellm()
        costs = store.get_all()
        assert len(costs) > 100
        for model, (inp, out) in list(costs.items())[:5]:
            assert isinstance(model, str)
            assert isinstance(inp, float)
            assert isinstance(out, float)


class TestLookupCost:
    """Tests for lookup_cost function with DB-backed data."""

    def test_known_model(self, populated_db: LocalDatabase) -> None:
        """Known model returns non-zero cost."""
        cost = lookup_cost("gpt-4o")
        assert cost.input_cost_per_token > 0
        assert cost.output_cost_per_token > 0

    def test_prefix_match_versioned_model(self, populated_db: LocalDatabase) -> None:
        """Versioned model name matches via prefix."""
        cost = lookup_cost("gpt-4o-2024-08-06-extended")
        # Should find some match (gpt-4o or gpt-4o-2024-08-06)
        assert cost.input_cost_per_token > 0

    def test_strips_provider_prefix(self, populated_db: LocalDatabase) -> None:
        """Provider prefix is stripped before lookup."""
        cost_with = lookup_cost("openai/gpt-4o")
        cost_without = lookup_cost("gpt-4o")
        assert cost_with == cost_without

    def test_unknown_model_returns_zero(self, populated_db: LocalDatabase) -> None:
        """Unknown model returns zero costs."""
        cost = lookup_cost("completely-unknown-model-xyz")
        assert cost.input_cost_per_token == 0.0
        assert cost.output_cost_per_token == 0.0

    def test_uninitialized_returns_zero(self) -> None:
        """When init() never called, lookups return zero."""
        # Clear the module-level cache to simulate uninitialized state
        saved = dict(_costs)
        _costs.clear()
        try:
            cost = lookup_cost("gpt-4o")
            assert cost.input_cost_per_token == 0.0
            assert cost.output_cost_per_token == 0.0
        finally:
            _costs.update(saved)

    def test_longest_prefix_wins(self, populated_db: LocalDatabase) -> None:
        """Longer prefix match takes priority over shorter one."""
        # gpt-4o-mini should not resolve to gpt-4o's price
        mini_cost = lookup_cost("gpt-4o-mini")
        base_cost = lookup_cost("gpt-4o")
        if mini_cost.input_cost_per_token > 0 and base_cost.input_cost_per_token > 0:
            assert mini_cost != base_cost


class TestCalculateCost:
    """Tests for calculate_cost function."""

    def test_basic_cost_calculation(self, populated_db: LocalDatabase) -> None:
        """Cost calculation multiplies tokens by per-token rates."""
        cost = calculate_cost("gpt-4o-mini", prompt_tokens=1000, completion_tokens=500)
        assert cost > 0

    def test_zero_tokens_zero_cost(self, populated_db: LocalDatabase) -> None:
        """Zero tokens produce zero cost."""
        cost = calculate_cost("gpt-4o", prompt_tokens=0, completion_tokens=0)
        assert cost == 0.0

    def test_unknown_model_zero_cost(self, populated_db: LocalDatabase) -> None:
        """Unknown model produces zero cost."""
        cost = calculate_cost("unknown-model", prompt_tokens=1000, completion_tokens=1000)
        assert cost == 0.0

    def test_cost_is_sum_of_input_and_output(self, populated_db: LocalDatabase) -> None:
        """Total cost equals input cost + output cost."""
        model = "gpt-4o"
        costs = lookup_cost(model)
        prompt_tokens = 1000
        completion_tokens = 500

        expected = (
            costs.input_cost_per_token * prompt_tokens
            + costs.output_cost_per_token * completion_tokens
        )
        actual = calculate_cost(model, prompt_tokens, completion_tokens)
        assert actual == pytest.approx(expected)

    def test_negative_tokens_clamped(self, populated_db: LocalDatabase) -> None:
        """Negative tokens are clamped to zero."""
        cost = calculate_cost("gpt-4o", prompt_tokens=-100, completion_tokens=-50)
        assert cost == 0.0


class TestModelCost:
    """Tests for ModelCost dataclass."""

    def test_frozen(self) -> None:
        """ModelCost is immutable."""
        cost = ModelCost(1.0, 2.0)
        with pytest.raises(AttributeError):
            cost.input_cost_per_token = 3.0  # type: ignore[misc]
