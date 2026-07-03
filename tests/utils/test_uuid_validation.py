"""Tests for UUID validation helpers."""

from gobby.utils.uuid_validation import is_full_uuid


def test_valid_36_character_uuid_returns_true() -> None:
    assert is_full_uuid("123e4567-e89b-12d3-a456-426614174000") is True


def test_uuid_prefix_returns_false() -> None:
    assert is_full_uuid("123e4567") is False


def test_hyphenless_uuid_returns_false() -> None:
    assert is_full_uuid("123e4567e89b12d3a456426614174000") is False


def test_empty_string_and_none_return_false() -> None:
    assert is_full_uuid("") is False
    assert is_full_uuid(None) is False
