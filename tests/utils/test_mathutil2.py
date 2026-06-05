"""Tests for mathutil2 helpers."""

import pytest

from gobby.utils.mathutil2 import multiply

pytestmark = pytest.mark.unit


def test_multiply_returns_product() -> None:
    assert multiply(3, 4) == 12
    assert multiply(0, 5) == 0
