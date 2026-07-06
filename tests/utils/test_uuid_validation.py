"""Tests for UUID validation helpers."""

import uuid

import pytest

from gobby.utils.uuid_validation import is_full_uuid, parse_uuid_reference

VALID_UUID = uuid.UUID("123e4567-e89b-12d3-a456-426614174000")


def test_valid_36_character_uuid_returns_true() -> None:
    assert is_full_uuid("123e4567-e89b-12d3-a456-426614174000") is True


def test_uuid_prefix_returns_false() -> None:
    assert is_full_uuid("123e4567") is False


def test_hyphenless_uuid_returns_false() -> None:
    assert is_full_uuid("123e4567e89b12d3a456426614174000") is False


def test_empty_string_and_none_return_false() -> None:
    assert is_full_uuid("") is False
    assert is_full_uuid(None) is False


@pytest.mark.parametrize("value", [None, "", 0, False])
def test_parse_uuid_reference_returns_none_for_falsy_values(value: object) -> None:
    assert parse_uuid_reference(value) is None


@pytest.mark.parametrize("value", [object(), 42, ["not-a-uuid"]])
def test_uuid_helpers_reject_invalid_non_string_objects(value: object) -> None:
    assert parse_uuid_reference(value) is None
    assert is_full_uuid(value) is False


def test_parse_uuid_reference_accepts_uuid_objects() -> None:
    assert parse_uuid_reference(VALID_UUID) == VALID_UUID


def test_is_full_uuid_rejects_uuid_objects() -> None:
    assert is_full_uuid(VALID_UUID) is False


def test_parse_uuid_reference_accepts_valid_uuid_strings() -> None:
    assert parse_uuid_reference(str(VALID_UUID)) == VALID_UUID


def test_parse_uuid_reference_rejects_invalid_uuid_strings() -> None:
    assert parse_uuid_reference("not-a-uuid") is None
