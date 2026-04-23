"""Focused coverage tests for task MCP tools."""


import pytest

pytestmark = pytest.mark.unit


class TestToolSchemas:
    """Tests for tool input schemas."""

    def test_create_task_schema_has_required_fields(self, task_registry) -> None:
        """Test create_task schema has required title field."""
        schema = task_registry.get_schema("create_task")

        assert schema is not None
        assert "title" in schema["inputSchema"]["properties"]
        assert "title" in schema["inputSchema"]["required"]

    def test_create_task_schema_has_claim_parameter(self, task_registry) -> None:
        """Test create_task schema includes optional claim parameter."""
        schema = task_registry.get_schema("create_task")

        assert schema is not None
        props = schema["inputSchema"]["properties"]

        assert "claim" in props, "Missing claim parameter in create_task schema"
        assert props["claim"]["type"] == "boolean"
        assert props["claim"]["default"] is False
        # claim should NOT be in required
        assert "claim" not in schema["inputSchema"]["required"]

    def test_create_task_schema_category_enum_includes_refactor(self, task_registry) -> None:
        """The create_task category enum must include refactor.

        Expansion emits refactor-category tasks (see expansion_service._build_phase_refactor_description);
        without the enum accepting it, every expansion round hits an MCP validation error.
        """
        schema = task_registry.get_schema("create_task")

        assert schema is not None
        category = schema["inputSchema"]["properties"]["category"]
        assert "refactor" in category["enum"]
        # Sanity: full canonical set must be present so docs/skills don't drift.
        assert set(category["enum"]) == {
            "code",
            "config",
            "docs",
            "refactor",
            "test",
            "research",
            "planning",
            "manual",
        }

    def test_update_task_schema_category_enum_includes_refactor(self, task_registry) -> None:
        """The update_task category enum must match create_task and accept refactor."""
        schema = task_registry.get_schema("update_task")

        assert schema is not None
        category = schema["inputSchema"]["properties"]["category"]
        assert "refactor" in category["enum"]
        assert set(category["enum"]) == {
            "code",
            "config",
            "docs",
            "refactor",
            "test",
            "research",
            "planning",
            "manual",
        }

    def test_update_task_schema_has_all_fields(self, task_registry) -> None:
        """Test update_task schema includes all updatable fields."""
        schema = task_registry.get_schema("update_task")

        assert schema is not None
        props = schema["inputSchema"]["properties"]

        expected_props = [
            "task_id",
            "title",
            "description",
            "status",
            "priority",
            "assignee",
            "labels",
            "validation_criteria",
            "parent_task_id",
            "category",
            "workflow_name",
            "verification",
            "sequence_order",
        ]

        for prop in expected_props:
            assert prop in props, f"Missing property: {prop}"

    def test_close_task_schema_has_all_fields(self, task_registry) -> None:
        """Test close_task schema includes all options."""
        schema = task_registry.get_schema("close_task")

        assert schema is not None
        props = schema["inputSchema"]["properties"]

        expected_props = [
            "task_id",
            "reason",
            "changes_summary",
            "skip_validation",
            "override_justification",
            "commit_sha",
        ]

        for prop in expected_props:
            assert prop in props, f"Missing property: {prop}"

    def test_close_task_schema_requires_changes_summary(self, task_registry) -> None:
        """Test close_task schema has changes_summary in properties (enforced at runtime)."""
        schema = task_registry.get_schema("close_task")

        assert schema is not None
        props = schema["inputSchema"]["properties"]
        assert "changes_summary" in props, "changes_summary must be in properties"

    def test_list_tasks_schema_has_filters(self, task_registry) -> None:
        """Test list_tasks schema includes filter options."""
        schema = task_registry.get_schema("list_tasks")

        assert schema is not None
        props = schema["inputSchema"]["properties"]

        expected_props = [
            "status",
            "priority",
            "task_type",
            "assignee",
            "label",
            "parent_task_id",
            "title_like",
            "limit",
            "all_projects",
        ]

        for prop in expected_props:
            assert prop in props, f"Missing property: {prop}"


# =============================================================================
# Session Variable Mirroring Tests (#9071)
# =============================================================================
