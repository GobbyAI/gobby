"""Tests for rule-specific query helpers on LocalWorkflowDefinitionManager."""

from __future__ import annotations

import json

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    """Create a fresh database with migrations applied."""
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _make_rule_json(event: str, effect_type: str = "block", group: str | None = None) -> str:
    body: dict = {
        "event": event,
        "effect": {"type": effect_type},
    }
    if group:
        body["group"] = group
    return json.dumps(body)


class TestListByWorkflowTypeRule:
    def test_list_rules_only(self, manager: LocalWorkflowDefinitionManager) -> None:
        """list_all(workflow_type='rule') returns only rule definitions."""
        manager.create(
            name="rule-a",
            definition_json=_make_rule_json("before_tool"),
            workflow_type="rule",
        )
        manager.create(
            name="workflow-a",
            definition_json=json.dumps({"name": "workflow-a"}),
            workflow_type="workflow",
        )

        rules = manager.list_all(workflow_type="rule")
        assert len(rules) == 1
        assert rules[0].name == "rule-a"
        assert rules[0].workflow_type == "rule"

    def test_list_rules_empty(self, manager: LocalWorkflowDefinitionManager) -> None:
        """list_all(workflow_type='rule') returns empty when no rules exist."""
        rules = manager.list_all(workflow_type="rule")
        # May include bundled workflows but no rules
        assert all(r.workflow_type == "rule" for r in rules)


class TestFilterByEvent:
    def test_filter_by_event_type(self, manager: LocalWorkflowDefinitionManager) -> None:
        """list_rules_by_event should filter by event type from definition_json."""
        manager.create(
            name="before-tool-rule",
            definition_json=_make_rule_json("before_tool"),
            workflow_type="rule",
        )
        manager.create(
            name="after-tool-rule",
            definition_json=_make_rule_json("after_tool"),
            workflow_type="rule",
        )
        manager.create(
            name="stop-rule",
            definition_json=_make_rule_json("stop"),
            workflow_type="rule",
        )

        results = manager.list_rules_by_event("before_tool")
        assert len(results) == 1
        assert results[0].name == "before-tool-rule"

    def test_filter_by_event_returns_multiple(
        self, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Multiple rules with same event type should all be returned."""
        manager.create(
            name="rule-1",
            definition_json=_make_rule_json("stop"),
            workflow_type="rule",
        )
        manager.create(
            name="rule-2",
            definition_json=_make_rule_json("stop"),
            workflow_type="rule",
        )

        results = manager.list_rules_by_event("stop")
        assert len(results) == 2

    def test_filter_by_event_empty(self, manager: LocalWorkflowDefinitionManager) -> None:
        """No rules for the given event should return empty list."""
        manager.create(
            name="before-tool-rule",
            definition_json=_make_rule_json("before_tool"),
            workflow_type="rule",
        )

        results = manager.list_rules_by_event("session_end")
        assert len(results) == 0


class TestFilterByGroup:
    def test_filter_by_group(self, manager: LocalWorkflowDefinitionManager) -> None:
        """list_rules_by_group should filter by group from definition_json."""
        manager.create(
            name="task-rule",
            definition_json=_make_rule_json("before_tool", group="task-enforcement"),
            workflow_type="rule",
        )
        manager.create(
            name="stop-rule",
            definition_json=_make_rule_json("stop", group="stop-gates"),
            workflow_type="rule",
        )

        results = manager.list_rules_by_group("task-enforcement")
        assert len(results) == 1
        assert results[0].name == "task-rule"

    def test_filter_by_group_returns_multiple(
        self, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Multiple rules in same group should all be returned."""
        manager.create(
            name="rule-a",
            definition_json=_make_rule_json("before_tool", group="enforcement"),
            workflow_type="rule",
        )
        manager.create(
            name="rule-b",
            definition_json=_make_rule_json("after_tool", group="enforcement"),
            workflow_type="rule",
        )

        results = manager.list_rules_by_group("enforcement")
        assert len(results) == 2

    def test_filter_by_group_empty(self, manager: LocalWorkflowDefinitionManager) -> None:
        """No rules in the given group should return empty list."""
        manager.create(
            name="task-rule",
            definition_json=_make_rule_json("before_tool", group="task-enforcement"),
            workflow_type="rule",
        )

        results = manager.list_rules_by_group("nonexistent-group")
        assert len(results) == 0

    def test_filter_excludes_null_group(self, manager: LocalWorkflowDefinitionManager) -> None:
        """Rules without a group should not be returned by group filter."""
        manager.create(
            name="no-group-rule",
            definition_json=_make_rule_json("before_tool"),
            workflow_type="rule",
        )

        results = manager.list_rules_by_group("task-enforcement")
        assert len(results) == 0


class TestCombinedFilters:
    def test_rules_respect_enabled_filter(self, manager: LocalWorkflowDefinitionManager) -> None:
        """Rule queries should respect enabled=True/False filtering."""
        manager.create(
            name="enabled-rule",
            definition_json=_make_rule_json("before_tool"),
            workflow_type="rule",
            enabled=True,
        )
        manager.create(
            name="disabled-rule",
            definition_json=_make_rule_json("before_tool"),
            workflow_type="rule",
            enabled=False,
        )

        enabled = manager.list_all(workflow_type="rule", enabled=True)
        disabled = manager.list_all(workflow_type="rule", enabled=False)

        enabled_names = [r.name for r in enabled]
        disabled_names = [r.name for r in disabled]

        assert "enabled-rule" in enabled_names
        assert "disabled-rule" not in enabled_names
        assert "disabled-rule" in disabled_names

    def test_rules_respect_soft_delete(self, manager: LocalWorkflowDefinitionManager) -> None:
        """Soft-deleted rules should be excluded by default."""
        row = manager.create(
            name="to-delete",
            definition_json=_make_rule_json("before_tool"),
            workflow_type="rule",
        )
        manager.delete(row.id)

        rules = manager.list_all(workflow_type="rule")
        names = [r.name for r in rules]
        assert "to-delete" not in names

        # But include_deleted=True should show it
        rules_incl = manager.list_all(workflow_type="rule", include_deleted=True)
        names_incl = [r.name for r in rules_incl]
        assert "to-delete" in names_incl
