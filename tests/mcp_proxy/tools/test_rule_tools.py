"""Tests for rule MCP tools.

Verifies that rule tools wrap LocalWorkflowDefinitionManager with
workflow_type='rule' filtering:
- list_rules: returns only rules, supports event/group/enabled filters
- get_rule: returns full rule definition by name
- toggle_rule: updates enabled flag
- create_rule: creates with workflow_type='rule'
- delete_rule: soft-deletes (bundled protected)
"""

from __future__ import annotations

import json

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_rule_tools.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def def_manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _create_test_rule(
    def_manager: LocalWorkflowDefinitionManager,
    name: str = "test-rule",
    event: str = "before_tool",
    group: str = "test-group",
    enabled: bool = True,
    source: str = "installed",
    tags: list[str] | None = None,
) -> str:
    """Create a test rule and return its ID."""
    body = {
        "event": event,
        "group": group,
        "effects": [{"type": "block", "reason": "test"}],
    }
    row = def_manager.create(
        name=name,
        definition_json=json.dumps(body),
        workflow_type="rule",
        enabled=enabled,
        source=source,
        tags=tags,
    )
    return row.id


def _create_test_workflow(
    def_manager: LocalWorkflowDefinitionManager, name: str = "test-wf"
) -> str:
    """Create a non-rule workflow to verify filtering."""
    row = def_manager.create(
        name=name,
        definition_json=json.dumps({"name": name}),
        workflow_type="workflow",
    )
    return row.id


@pytest.fixture
def rule_tools(db, def_manager):
    """Create the rule tools module functions."""
    from gobby.mcp_proxy.tools.workflows._rules import (
        create_rule,
        delete_rule,
        get_rule,
        list_rules,
        toggle_rule,
        update_rule,
    )

    return {
        "list_rules": lambda **kw: list_rules(def_manager, **kw),
        "get_rule": lambda **kw: get_rule(def_manager, **kw),
        "toggle_rule": lambda **kw: toggle_rule(def_manager, **kw),
        "create_rule": lambda **kw: create_rule(def_manager, **kw),
        "delete_rule": lambda **kw: delete_rule(def_manager, **kw),
        "update_rule": lambda **kw: update_rule(def_manager, **kw),
    }


# ═══════════════════════════════════════════════════════════════════════
# list_rules
# ═══════════════════════════════════════════════════════════════════════


class TestListRules:
    """list_rules returns only rules (not workflows/pipelines)."""

    def test_returns_only_rules(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="my-rule")
        _create_test_workflow(def_manager, name="my-workflow")

        result = rule_tools["list_rules"]()
        assert result["success"] is True
        names = [r["name"] for r in result["rules"]]
        assert "my-rule" in names
        assert "my-workflow" not in names

    def test_filter_by_event(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="rule-before", event="before_tool")
        _create_test_rule(def_manager, name="rule-after", event="after_tool")

        result = rule_tools["list_rules"](event="before_tool")
        assert result["success"] is True
        names = [r["name"] for r in result["rules"]]
        assert "rule-before" in names
        assert "rule-after" not in names

    def test_filter_by_group(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="rule-a", group="alpha")
        _create_test_rule(def_manager, name="rule-b", group="beta")

        result = rule_tools["list_rules"](group="alpha")
        assert result["success"] is True
        names = [r["name"] for r in result["rules"]]
        assert "rule-a" in names
        assert "rule-b" not in names

    def test_filter_by_enabled(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="enabled-rule", enabled=True)
        _create_test_rule(def_manager, name="disabled-rule", enabled=False)

        result = rule_tools["list_rules"](enabled=True)
        assert result["success"] is True
        names = [r["name"] for r in result["rules"]]
        assert "enabled-rule" in names
        assert "disabled-rule" not in names

    def test_returns_count(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="r1")
        _create_test_rule(def_manager, name="r2")

        result = rule_tools["list_rules"]()
        assert result["count"] == 2

    def test_brief_returns_minimal_fields(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="verbose-rule", event="stop", group="g1")

        result = rule_tools["list_rules"](brief=True)
        assert result["success"] is True
        rule = result["rules"][0]
        assert set(rule.keys()) == {"name", "event", "group", "enabled"}
        assert rule["name"] == "verbose-rule"
        assert rule["event"] == "stop"
        assert rule["group"] == "g1"
        assert rule["enabled"] is True


# ═══════════════════════════════════════════════════════════════════════
# get_rule
# ═══════════════════════════════════════════════════════════════════════


class TestGetRule:
    """get_rule returns full rule definition by name."""

    def test_returns_definition(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="my-rule", event="stop", group="test")

        result = rule_tools["get_rule"](name="my-rule")
        assert result["success"] is True
        assert result["rule"]["name"] == "my-rule"
        assert result["rule"]["event"] == "stop"
        assert result["rule"]["group"] == "test"

    def test_not_found(self, rule_tools) -> None:
        result = rule_tools["get_rule"](name="nonexistent")
        assert result["success"] is False
        assert "error" in result

    def test_returns_enabled_status(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="disabled-rule", enabled=False)

        result = rule_tools["get_rule"](name="disabled-rule")
        assert result["success"] is True
        assert result["rule"]["enabled"] is False


# ═══════════════════════════════════════════════════════════════════════
# toggle_rule
# ═══════════════════════════════════════════════════════════════════════


class TestToggleRule:
    """toggle_rule updates enabled flag."""

    def test_disable_rule(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="my-rule", enabled=True)

        result = rule_tools["toggle_rule"](name="my-rule", enabled=False)
        assert result["success"] is True
        assert result["rule"]["enabled"] is False

        # Verify in DB
        row = def_manager.get_by_name("my-rule")
        assert row.enabled is False

    def test_enable_rule(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="my-rule", enabled=False)

        result = rule_tools["toggle_rule"](name="my-rule", enabled=True)
        assert result["success"] is True
        assert result["rule"]["enabled"] is True

    def test_not_found(self, rule_tools) -> None:
        result = rule_tools["toggle_rule"](name="nonexistent", enabled=True)
        assert result["success"] is False


# ═══════════════════════════════════════════════════════════════════════
# update_rule
# ═══════════════════════════════════════════════════════════════════════


class TestUpdateRule:
    """update_rule mirrors PUT /api/rules/{name}: partial field updates."""

    def test_update_description(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="my-rule")

        result = rule_tools["update_rule"](name="my-rule", description="Updated")
        assert result["success"] is True
        assert result["rule"]["description"] == "Updated"

        row = def_manager.get_by_name("my-rule")
        assert row.description == "Updated"

    def test_update_priority(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="my-rule")

        result = rule_tools["update_rule"](name="my-rule", priority=42)
        assert result["success"] is True
        assert result["rule"]["priority"] == 42

    def test_update_tags(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="my-rule", tags=["old"])

        result = rule_tools["update_rule"](name="my-rule", tags=["new", "user"])
        assert result["success"] is True
        assert result["rule"]["tags"] == ["new", "user"]

        row = def_manager.get_by_name("my-rule")
        assert row.tags == ["new", "user"]

    def test_update_enabled(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="my-rule", enabled=True)

        result = rule_tools["update_rule"](name="my-rule", enabled=False)
        assert result["success"] is True
        assert result["rule"]["enabled"] is False

        row = def_manager.get_by_name("my-rule")
        assert row.enabled is False

    def test_update_definition_replaces_body(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="my-rule", event="before_tool")

        new_body = {
            "event": "turn_end",
            "group": "stop-gates",
            "effects": [{"type": "block", "reason": "replaced"}],
        }
        result = rule_tools["update_rule"](name="my-rule", definition=new_body)
        assert result["success"] is True
        assert result["rule"]["event"] == "turn_end"
        assert result["rule"]["effects"] == [{"type": "block", "reason": "replaced"}]

        row = def_manager.get_by_name("my-rule")
        body = json.loads(row.definition_json)
        assert body["event"] == "turn_end"
        assert body["effects"] == [{"type": "block", "reason": "replaced"}]

    def test_update_definition_invalid(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="my-rule")

        result = rule_tools["update_rule"](
            name="my-rule",
            definition={"event": "turn_end"},  # missing required effects
        )
        assert result["success"] is False
        assert "invalid rule definition" in result["error"].lower()

    def test_update_definition_extracts_metadata(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="my-rule")

        body_with_meta = {
            "event": "turn_end",
            "effects": [{"type": "block", "reason": "x"}],
            "description": "From body",
            "priority": 7,
        }
        result = rule_tools["update_rule"](name="my-rule", definition=body_with_meta)
        assert result["success"] is True
        assert result["rule"]["description"] == "From body"
        assert result["rule"]["priority"] == 7

        # Metadata should be hoisted off the body, not duplicated inside it.
        row = def_manager.get_by_name("my-rule")
        stored_body = json.loads(row.definition_json)
        assert "description" not in stored_body
        assert "priority" not in stored_body

    def test_update_definition_explicit_metadata_wins(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="my-rule")

        result = rule_tools["update_rule"](
            name="my-rule",
            description="Explicit",
            definition={
                "event": "turn_end",
                "effects": [{"type": "block", "reason": "x"}],
                "description": "From body",
            },
        )
        assert result["success"] is True
        assert result["rule"]["description"] == "Explicit"

    def test_update_no_fields(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="my-rule")

        result = rule_tools["update_rule"](name="my-rule")
        assert result["success"] is False
        assert "no fields" in result["error"].lower()

    def test_update_not_found(self, rule_tools) -> None:
        result = rule_tools["update_rule"](name="nonexistent", description="x")
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_update_skips_workflows(self, def_manager, rule_tools) -> None:
        """Workflows (non-rule rows) should not be updatable through update_rule."""
        _create_test_workflow(def_manager, name="my-wf")

        result = rule_tools["update_rule"](name="my-wf", description="x")
        assert result["success"] is False
        assert "not found" in result["error"].lower()


# ═══════════════════════════════════════════════════════════════════════
# create_rule
# ═══════════════════════════════════════════════════════════════════════


class TestCreateRule:
    """create_rule creates with workflow_type='rule'."""

    def test_creates_rule(self, def_manager, rule_tools) -> None:
        body = {
            "event": "before_tool",
            "group": "my-group",
            "effects": [{"type": "block", "reason": "testing"}],
        }
        result = rule_tools["create_rule"](
            name="new-rule",
            definition=body,
        )
        assert result["success"] is True
        assert result["rule"]["name"] == "new-rule"

        # Verify it's stored as workflow_type='rule'
        row = def_manager.get_by_name("new-rule")
        assert row is not None
        assert row.workflow_type == "rule"

    def test_validates_definition(self, rule_tools) -> None:
        """Should reject invalid rule definitions."""
        result = rule_tools["create_rule"](
            name="bad-rule",
            definition={"not_valid": True},
        )
        assert result["success"] is False

    def test_duplicate_name_rejected(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="existing-rule")

        result = rule_tools["create_rule"](
            name="existing-rule",
            definition={
                "event": "stop",
                "effects": [{"type": "block", "reason": "dup"}],
            },
        )
        assert result["success"] is False
        assert "exists" in result["error"].lower() or "already" in result["error"].lower()


# ═══════════════════════════════════════════════════════════════════════
# delete_rule
# ═══════════════════════════════════════════════════════════════════════


class TestDeleteRule:
    """delete_rule soft-deletes (bundled protected)."""

    def test_deletes_user_rule(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="custom-rule", tags=["user"])

        result = rule_tools["delete_rule"](name="custom-rule")
        assert result["success"] is True

        # Verify soft-deleted
        row = def_manager.get_by_name("custom-rule")
        assert row is None  # Not visible without include_deleted

    def test_protects_bundled_rule(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="bundled-rule", tags=["gobby"])

        result = rule_tools["delete_rule"](name="bundled-rule")
        assert result["success"] is False
        assert "bundled" in result["error"].lower()

    def test_force_deletes_bundled(self, def_manager, rule_tools) -> None:
        _create_test_rule(def_manager, name="bundled-rule", tags=["gobby"])

        result = rule_tools["delete_rule"](name="bundled-rule", force=True)
        assert result["success"] is True

    def test_not_found(self, rule_tools) -> None:
        result = rule_tools["delete_rule"](name="nonexistent")
        assert result["success"] is False
