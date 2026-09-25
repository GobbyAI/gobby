"""Cache behavior for context-independent safe-expression syntax trees."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest

from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clear_expression_caches() -> Iterator[None]:
    SafeExpressionEvaluator._normalize_expr.cache_clear()
    SafeExpressionEvaluator._parse_normalized_expr.cache_clear()
    yield
    SafeExpressionEvaluator._normalize_expr.cache_clear()
    SafeExpressionEvaluator._parse_normalized_expr.cache_clear()


def test_reuses_one_parse_for_normalized_expression_with_current_context() -> None:
    with patch("gobby.workflows.safe_evaluator.ast.parse", wraps=ast.parse) as parse:
        assert SafeExpressionEvaluator({"value": 2}, {}).evaluate("value == 2") is True
        assert SafeExpressionEvaluator({"value": 3}, {}).evaluate("value  ==  2") is False
        assert SafeExpressionEvaluator({"value": 2}, {}).evaluate_value("value == 2") is True

    assert parse.call_count == 1


def test_distinct_malformed_and_rejected_expressions_keep_error_semantics() -> None:
    evaluator = SafeExpressionEvaluator({"value": 1}, {})
    with patch("gobby.workflows.safe_evaluator.ast.parse", wraps=ast.parse) as parse:
        assert evaluator.evaluate("value == 1") is True
        assert evaluator.evaluate("value == 2") is False
        for _ in range(2):
            with pytest.raises(ValueError, match="Invalid expression: invalid syntax"):
                evaluator.evaluate("value ==")
            with pytest.raises(ValueError, match="Unsupported binary operator: Mult"):
                evaluator.evaluate("value * 2")

    assert parse.call_count == 5


def test_eviction_reparses_without_changing_result() -> None:
    cache_size = SafeExpressionEvaluator._parse_normalized_expr.cache_info().maxsize
    assert cache_size is not None
    evaluator = SafeExpressionEvaluator({}, {})
    with patch("gobby.workflows.safe_evaluator.ast.parse", wraps=ast.parse) as parse:
        for value in range(cache_size + 1):
            assert evaluator.evaluate_value(str(value)) == value
        assert evaluator.evaluate_value("0") == 0

    assert parse.call_count == cache_size + 2
    assert SafeExpressionEvaluator._parse_normalized_expr.cache_info().currsize == cache_size


def test_concurrent_evaluators_keep_context_separate() -> None:
    barrier = Barrier(4)

    def release() -> bool:
        barrier.wait(timeout=10)
        return True

    def evaluate(value: int) -> int:
        evaluator = SafeExpressionEvaluator({"value": value}, {"release": release})
        result = evaluator.evaluate_value("release() and value")
        assert isinstance(result, int)
        return result

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(evaluate, range(4))) == [0, 1, 2, 3]
