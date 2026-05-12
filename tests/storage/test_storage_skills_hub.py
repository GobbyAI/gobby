"""Tests for skill hub tracking fields."""

import pytest

from gobby.storage.skills import Skill

pytestmark = pytest.mark.unit


class TestSkillHubFields:
    """Tests for hub tracking fields in the Skill dataclass."""

    def test_hub_fields_default_to_none(self) -> None:
        """Test that hub tracking fields default to None."""
        skill = Skill(
            id="skl-123",
            name="test-skill",
            description="A test skill",
            content="# Test Skill",
        )

        # These should exist and be None
        assert skill.hub_name is None
        assert skill.hub_slug is None
        assert skill.hub_version is None

    def test_skill_to_dict_includes_hub_fields(self) -> None:
        """Test that to_dict includes hub tracking fields."""
        skill = Skill(
            id="skl-123",
            name="test-skill",
            description="A test skill",
            content="# Test Skill",
            hub_name="Gobby Hub",
            hub_slug="gobby-hub",
            hub_version="1.2.3",
        )

        d = skill.to_dict()

        assert d["hub_name"] == "Gobby Hub"
        assert d["hub_slug"] == "gobby-hub"
        assert d["hub_version"] == "1.2.3"


class MockRow:
    def __init__(self, data):
        self.data = data

    def __getitem__(self, key):
        return self.data[key]

    def keys(self):
        return list(self.data.keys())


def test_skill_from_row_with_hub_fields() -> None:
    """Test from_row with hub fields using a MockRow."""
    row_data = {
        "id": "skl-123",
        "name": "test-skill",
        "description": "desc",
        "content": "content",
        "version": "1.0.0",
        "license": "MIT",
        "compatibility": None,
        "allowed_tools": None,
        "metadata": None,
        "source_path": None,
        "source_type": "local",
        "source_ref": None,
        "enabled": 1,
        "project_id": None,
        "created_at": "2024-01-01",
        "updated_at": "2024-01-01",
        "hub_name": "Gobby Hub",
        "hub_slug": "gobby-hub",
        "hub_version": "1.2.3",
    }
    row = MockRow(row_data)

    skill = Skill.from_row(row)

    assert skill.hub_name == "Gobby Hub"
    assert skill.hub_slug == "gobby-hub"
    assert skill.hub_version == "1.2.3"
