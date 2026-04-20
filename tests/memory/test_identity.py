"""Tests for stable entity-key encoding."""

from __future__ import annotations

import pytest

from gobby.memory.identity import entity_key, normalize_entity_name

pytestmark = pytest.mark.unit


def test_entity_key_distinguishes_global_scope_from_matching_project_id() -> None:
    """A real project_id matching the global marker should not collide."""
    assert entity_key(None, "Python") != entity_key("__global__", "Python")


def test_entity_key_is_unambiguous_when_components_contain_separators() -> None:
    """Embedded separators should not create ambiguous keys."""
    assert entity_key("proj::1", "A::B") != entity_key("proj", "1::a::b")


def test_normalize_entity_name_rejects_empty_values() -> None:
    """Empty or whitespace-only names should fail normalization."""
    with pytest.raises(ValueError, match="entity name must be non-empty"):
        normalize_entity_name("   ")
