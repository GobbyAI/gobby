"""Tests for fibonacci utility."""

import pytest

from gobby.utils.fibonacci import fibonacci

pytestmark = pytest.mark.unit


class TestFibonacci:
    """Tests for fibonacci function."""

    def test_fibonacci_zero(self) -> None:
        """Test fibonacci(0) returns 0."""
        assert fibonacci(0) == 0

    def test_fibonacci_one(self) -> None:
        """Test fibonacci(1) returns 1."""
        assert fibonacci(1) == 1

    def test_fibonacci_sequence(self) -> None:
        """Test fibonacci function with values n=0..12."""
        expected = [0, 1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144]
        for n in range(13):
            result = fibonacci(n)
            assert result == expected[n], f"fibonacci({n}) = {result}, expected {expected[n]}"

    def test_fibonacci_specific_values(self) -> None:
        """Test specific fibonacci values."""
        assert fibonacci(5) == 5
        assert fibonacci(6) == 8
        assert fibonacci(7) == 13
        assert fibonacci(10) == 55
        assert fibonacci(12) == 144
