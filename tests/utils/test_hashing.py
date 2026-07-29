"""Tests for shared hash validators."""

from __future__ import annotations

import pytest

from gobby.utils.hashing import is_sha256


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a" * 64, True),
        ("A" * 64, False),
        ("a" * 63, False),
        ("g" * 64, False),
        (None, False),
    ],
)
def test_is_sha256_requires_canonical_lowercase_hex(value: object, expected: bool) -> None:
    assert is_sha256(value) is expected
