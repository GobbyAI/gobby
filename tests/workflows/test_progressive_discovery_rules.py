"""Tests for progressive-discovery.yaml rules.

Verifies blocking rules enforce progressive discovery (list_mcp_servers →
list_tools → get_tool_schema → call_tool), tracker rules record state,
and reset rules clear state on context loss.

Includes integration tests that exercise the full RuleEngine.evaluate() flow
to verify conditions like is_server_listed actually resolve correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.rule_engine import RuleEngine
from gobby.workflows.sync import sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_progressive_discovery.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _sync_bundled(db):
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    # Mark templates as installed so get_by_name() finds them without include_templates
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")
    return result


PROGRESSIVE_DISCLOSURE_RULES = {
    "require-servers-listed",
    "require-server-listed-for-schema",
    "require-schema-before-call",
    "preseed-progressive-discovery",
    "track-schema-lookup",
    "track-servers-listed",
    "track-listed-servers",
    "reset-progressive-discovery",
}


class TestProgressiveDisclosureSync:
    """Test that progressive-discovery.yaml syncs correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All progressive-discovery rules should sync to workflow_definitions."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        rule_names = {r.name for r in rules}

        assert PROGRESSIVE_DISCLOSURE_RULES.issubset(
            rule_names
        ), f"Missing: {PROGRESSIVE_DISCLOSURE_RULES - rule_names}"

    def test_all_rules_have_progressive_discovery_tag(self, db, manager) -> None:
        """All rules should be tagged with 'progressive-discovery'."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in PROGRESSIVE_DISCLOSURE_RULES:
                assert (
                    row.tags and "progressive-discovery" in row.tags
                ), f"{row.name} missing 'progressive-discovery' tag"

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in PROGRESSIVE_DISCLOSURE_RULES:
                body = RuleDefinitionBody.model_validate_json(row.definition_json)
                for effect in body.resolved_effects:
                    assert effect.type in {"block", "set_variable", "inject_context"}


class TestRequireServersListed:
    """Verify require-servers-listed blocks list_tools without list_mcp_servers."""

    def test_blocks_list_tools(self, db, manager) -> None:
        """Should block mcp__gobby__list_tools."""
        _sync_bundled(db)

        row = manager.get_by_name("require-servers-listed")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effect.type == "block"
        assert "mcp__gobby__list_tools" in body.effect.tools

    def test_when_checks_servers_listed(self, db, manager) -> None:
        """Should check enforce_tool_schema_check and servers_listed."""
        _sync_bundled(db)

        row = manager.get_by_name("require-servers-listed")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "servers_listed" in body.when
        assert "enforce_tool_schema_check" in body.when


class TestRequireServerListedForSchema:
    """Verify require-server-listed-for-schema blocks get_tool_schema."""

    def test_blocks_get_tool_schema(self, db, manager) -> None:
        """Should block mcp__gobby__get_tool_schema."""
        _sync_bundled(db)

        row = manager.get_by_name("require-server-listed-for-schema")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effect.type == "block"
        assert "mcp__gobby__get_tool_schema" in body.effect.tools

    def test_when_checks_is_server_listed(self, db, manager) -> None:
        """Should use is_server_listed helper."""
        _sync_bundled(db)

        row = manager.get_by_name("require-server-listed-for-schema")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "is_server_listed" in body.when


class TestRequireSchemaBeforeCall:
    """Verify require-schema-before-call nudges (inject_context) for call_tool without schema."""

    def test_nudges_call_tool(self, db, manager) -> None:
        """Should inject_context for call_tool without schema."""
        _sync_bundled(db)

        row = manager.get_by_name("require-schema-before-call")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effect.type == "inject_context"
        assert body.effect.template is not None
        assert "schema" in body.effect.template.lower()

    def test_when_checks_tool_unlocked(self, db, manager) -> None:
        """Should check is_tool_unlocked, is_discovery_tool, and call_tool."""
        _sync_bundled(db)

        row = manager.get_by_name("require-schema-before-call")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "is_tool_unlocked" in body.when
        assert "is_discovery_tool" in body.when
        assert "call_tool" in body.when


class TestTrackSchemaLookup:
    """Verify track-schema-lookup records schema lookups."""

    def test_is_set_variable_effect(self, db, manager) -> None:
        """Should use set_variable to track unlocked_tools."""
        _sync_bundled(db)

        row = manager.get_by_name("track-schema-lookup")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "after_tool"
        assert body.effect.type == "set_variable"
        assert body.effect.variable == "unlocked_tools"

    def test_when_matches_get_tool_schema(self, db, manager) -> None:
        """Should fire on get_tool_schema calls."""
        _sync_bundled(db)

        row = manager.get_by_name("track-schema-lookup")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "get_tool_schema" in body.when


class TestTrackServersListed:
    """Verify track-servers-listed marks servers as listed."""

    def test_sets_servers_listed(self, db, manager) -> None:
        """Should set servers_listed to true."""
        _sync_bundled(db)

        row = manager.get_by_name("track-servers-listed")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "after_tool"
        assert body.effect.type == "set_variable"
        assert body.effect.variable == "servers_listed"
        assert body.effect.value is True

    def test_when_matches_list_mcp_servers(self, db, manager) -> None:
        """Should fire on list_mcp_servers calls."""
        _sync_bundled(db)

        row = manager.get_by_name("track-servers-listed")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "list_mcp_servers" in body.when


class TestTrackListedServers:
    """Verify track-listed-servers records server names."""

    def test_sets_listed_servers(self, db, manager) -> None:
        """Should set listed_servers variable."""
        _sync_bundled(db)

        row = manager.get_by_name("track-listed-servers")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "after_tool"
        assert body.effect.type == "set_variable"
        assert body.effect.variable == "listed_servers"

    def test_when_matches_list_tools(self, db, manager) -> None:
        """Should fire on list_tools calls."""
        _sync_bundled(db)

        row = manager.get_by_name("track-listed-servers")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "list_tools" in body.when


class TestResetRules:
    """Verify reset-progressive-discovery multi-effect rule clears all state on context loss."""

    def test_resets_all_three_variables(self, db, manager) -> None:
        """Should reset unlocked_tools, servers_listed, and listed_servers."""
        _sync_bundled(db)

        row = manager.get_by_name("reset-progressive-discovery")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "session_start"

        effects = body.resolved_effects
        assert len(effects) == 3

        vars_and_values = {e.variable: e.value for e in effects}
        assert vars_and_values["unlocked_tools"] == []
        assert vars_and_values["servers_listed"] is False
        assert vars_and_values["listed_servers"] == []

    def test_resets_fire_on_clear_compact_resume(self, db, manager) -> None:
        """Reset rule should fire on clear, compact, and conditional resume."""
        _sync_bundled(db)

        row = manager.get_by_name("reset-progressive-discovery")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "clear" in body.when
        assert "compact" in body.when


class TestPreseedProgressiveDiscovery:
    """Verify preseed-progressive-discovery sets internal server state on session start."""

    def test_preseed_rule_syncs(self, db, manager) -> None:
        """Preseed rule should sync to workflow_definitions."""
        _sync_bundled(db)

        row = manager.get_by_name("preseed-progressive-discovery")
        assert row is not None

    def test_preseed_fires_at_priority_81(self, db, manager) -> None:
        """Preseed should fire after reset (priority 80)."""
        _sync_bundled(db)

        row = manager.get_by_name("preseed-progressive-discovery")
        assert row.priority == 81

        reset_row = manager.get_by_name("reset-progressive-discovery")
        assert row.priority > reset_row.priority

    def test_preseed_sets_correct_variables(self, db, manager) -> None:
        """Preseed should set servers_listed=true and listed_servers with internal servers."""
        _sync_bundled(db)

        row = manager.get_by_name("preseed-progressive-discovery")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "session_start"

        effects = body.resolved_effects
        assert len(effects) == 2

        vars_and_values = {e.variable: e.value for e in effects}
        assert vars_and_values["servers_listed"] is True
        assert isinstance(vars_and_values["listed_servers"], list)
        assert "gobby-tasks" in vars_and_values["listed_servers"]
        assert "gobby-memory" in vars_and_values["listed_servers"]
        assert len(vars_and_values["listed_servers"]) == 16

    def test_preseed_has_no_when_condition(self, db, manager) -> None:
        """Preseed should fire on every session_start (no when condition)."""
        _sync_bundled(db)

        row = manager.get_by_name("preseed-progressive-discovery")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is None


class TestPriorityOrdering:
    """Verify priority ordering within progressive-discovery group."""

    def test_blocks_ordered_before_trackers(self, db, manager) -> None:
        """Block rules should have lower or equal priority numbers to tracker rules."""
        _sync_bundled(db)

        block_rule = manager.get_by_name("require-servers-listed")
        tracker_rule = manager.get_by_name("track-schema-lookup")

        assert block_rule is not None
        assert tracker_rule is not None
        assert block_rule.priority <= tracker_rule.priority


def _make_hook_event(
    event_type: HookEventType,
    tool_name: str = "",
    tool_input: dict | None = None,
) -> HookEvent:
    """Create a HookEvent for testing."""
    data = {"tool_name": tool_name}
    if tool_input is not None:
        data["tool_input"] = tool_input
    return HookEvent(
        event_type=event_type,
        session_id="test-session-ext",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": "test-session"},
    )


class TestRuleEngineIntegration:
    """End-to-end tests: RuleEngine.evaluate() with progressive discovery rules.

    These test the actual condition evaluation path including is_server_listed,
    is_tool_unlocked, and is_discovery_tool — the functions that were missing
    from allowed_funcs (the bug that caused permanent blocking).
    """

    @pytest.fixture
    def engine(self, db) -> RuleEngine:
        _sync_bundled(db)
        # Disable all rules first, then enable only the progressive discovery rules
        # (avoids interference from other rules that may have been synced as enabled)
        db.execute("UPDATE workflow_definitions SET enabled = 0")
        for name in PROGRESSIVE_DISCLOSURE_RULES:
            db.execute(
                "UPDATE workflow_definitions SET enabled = 1 WHERE name = ?",
                (name,),
            )
        return RuleEngine(db)

    @pytest.mark.asyncio
    async def test_get_tool_schema_blocked_before_list_tools(self, engine) -> None:
        """get_tool_schema should be blocked when server not yet listed."""
        variables = {"enforce_tool_schema_check": True, "listed_servers": []}
        event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "create_task"},
        )
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "block"

    @pytest.mark.asyncio
    async def test_get_tool_schema_allowed_after_list_tools(self, engine) -> None:
        """get_tool_schema should be allowed after list_tools was called for that server."""
        variables = {
            "enforce_tool_schema_check": True,
            "listed_servers": ["gobby-tasks"],
        }
        event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "create_task"},
        )
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "allow"

    @pytest.mark.asyncio
    async def test_call_tool_nudged_before_schema_lookup(self, engine) -> None:
        """call_tool should be allowed with nudge context when schema not yet looked up."""
        variables = {
            "enforce_tool_schema_check": True,
            "unlocked_tools": [],
        }
        event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "test"},
            },
        )
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "allow"
        assert result.context  # should have nudge text

    @pytest.mark.asyncio
    async def test_call_tool_allowed_after_schema_lookup(self, engine) -> None:
        """call_tool should be allowed after get_tool_schema was called."""
        variables = {
            "enforce_tool_schema_check": True,
            "unlocked_tools": ["gobby-tasks:create_task"],
        }
        event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "test"},
            },
        )
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "allow"

    @pytest.mark.asyncio
    async def test_call_tool_allowed_for_discovery_tools(self, engine) -> None:
        """call_tool should allow discovery tools (list_tools, etc.) without schema."""
        variables = {
            "enforce_tool_schema_check": True,
            "unlocked_tools": [],
        }
        event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "list_tools",
                "arguments": {"server_name": "gobby-tasks"},
            },
        )
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "allow"

    @pytest.mark.asyncio
    async def test_block_reason_renders_jinja_template(self, engine) -> None:
        """Block reason should render Jinja templates with tool_input values."""
        variables = {"enforce_tool_schema_check": True, "listed_servers": []}
        event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "create_task"},
        )
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "block"
        # The reason should contain the actual server name, not raw Jinja
        assert "gobby-tasks" in result.reason
        assert "{{" not in result.reason

    @pytest.mark.asyncio
    async def test_full_disclosure_flow(self, engine) -> None:
        """Full flow: block → track list_tools → allow get_tool_schema."""
        variables: dict = {
            "enforce_tool_schema_check": True,
            "listed_servers": [],
            "unlocked_tools": [],
        }

        # Step 1: get_tool_schema blocked (server not listed)
        event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "create_task"},
        )
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "block"

        # Step 2: simulate list_tools tracking (Python handler sets this)
        variables["listed_servers"].append("gobby-tasks")

        # Step 3: get_tool_schema now allowed
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "allow"

        # Step 4: call_tool allowed with nudge (schema not looked up)
        call_event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "test"},
            },
        )
        result = await engine.evaluate(call_event, "test-session", variables)
        assert result.decision == "allow"
        assert result.context  # nudge about missing schema

        # Step 5: simulate schema unlock (Python handler sets this)
        variables["unlocked_tools"].append("gobby-tasks:create_task")

        # Step 6: call_tool allowed without nudge (schema was looked up)
        result = await engine.evaluate(call_event, "test-session", variables)
        assert result.decision == "allow"

    @pytest.mark.asyncio
    async def test_tracking_rules_set_variables_via_after_tool(self, engine) -> None:
        """Tracking rules should set variables when after_tool events fire.

        Regression test: previously, the tracking rules had truncated `when`
        conditions that only checked `event.data.mcp_tool` (set by CLI adapters)
        but not `event.data.tool_name` (set by the web chat SDK bridge). This
        meant tracking never fired for web chat sessions.
        """
        variables: dict = {
            "enforce_tool_schema_check": True,
        }

        # Step 1: Fire after_tool for list_mcp_servers (native tool name)
        after_list_servers = _make_hook_event(
            HookEventType.AFTER_TOOL,
            tool_name="mcp__gobby__list_mcp_servers",
        )
        result = await engine.evaluate(after_list_servers, "test-session", variables)
        assert result.decision == "allow"
        assert (
            variables.get("servers_listed") is True
        ), "track-servers-listed should set servers_listed=True"

        # Step 2: Fire after_tool for list_tools (native tool name)
        after_list_tools = _make_hook_event(
            HookEventType.AFTER_TOOL,
            tool_name="mcp__gobby__list_tools",
            tool_input={"server_name": "gobby-tasks"},
        )
        result = await engine.evaluate(after_list_tools, "test-session", variables)
        assert result.decision == "allow"
        assert "gobby-tasks" in variables.get(
            "listed_servers", []
        ), "track-listed-servers should append server to listed_servers"

        # Step 3: Fire after_tool for get_tool_schema (native tool name)
        after_schema = _make_hook_event(
            HookEventType.AFTER_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "create_task"},
        )
        result = await engine.evaluate(after_schema, "test-session", variables)
        assert result.decision == "allow"
        assert "gobby-tasks:create_task" in variables.get(
            "unlocked_tools", []
        ), "track-schema-lookup should append server:tool to unlocked_tools"

    @pytest.mark.asyncio
    async def test_full_round_trip_via_rule_engine(self, engine) -> None:
        """Full round-trip: tracking rules fire on after_tool, unblocking before_tool.

        This is the end-to-end test that proves the YAML rules work without
        any Python handler assistance. All variable tracking is done by the
        rule engine's set_variable effects.
        """
        variables: dict = {
            "enforce_tool_schema_check": True,
        }

        # get_tool_schema is blocked (no list_tools yet)
        schema_event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "create_task"},
        )
        result = await engine.evaluate(schema_event, "test-session", variables)
        assert result.decision == "block"

        # Simulate list_tools completing (after_tool fires tracking rule)
        after_list_tools = _make_hook_event(
            HookEventType.AFTER_TOOL,
            tool_name="mcp__gobby__list_tools",
            tool_input={"server_name": "gobby-tasks"},
        )
        await engine.evaluate(after_list_tools, "test-session", variables)

        # Now get_tool_schema is allowed
        result = await engine.evaluate(schema_event, "test-session", variables)
        assert result.decision == "allow"

        # call_tool allowed with nudge (no schema lookup yet)
        call_event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "test"},
            },
        )
        result = await engine.evaluate(call_event, "test-session", variables)
        assert result.decision == "allow"
        assert result.context  # nudge about missing schema

        # Simulate get_tool_schema completing (after_tool fires tracking rule)
        after_schema = _make_hook_event(
            HookEventType.AFTER_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "create_task"},
        )
        await engine.evaluate(after_schema, "test-session", variables)

        # call_tool allowed without nudge (schema was looked up)
        result = await engine.evaluate(call_event, "test-session", variables)
        assert result.decision == "allow"

    @pytest.mark.asyncio
    async def test_preseed_then_get_schema_allowed(self, engine) -> None:
        """With pre-seeded state, get_tool_schema is allowed without list_tools."""
        # Simulate pre-seeded variables (as the preseed rule would set them)
        variables: dict = {
            "enforce_tool_schema_check": True,
            "servers_listed": True,
            "listed_servers": [
                "gobby-tasks",
                "gobby-sessions",
                "gobby-memory",
                "gobby-workflows",
                "gobby-canvas",
                "gobby-metrics",
                "gobby-agents",
                "gobby-worktrees",
                "gobby-clones",
                "gobby-merge",
                "gobby-hub",
                "gobby-config",
                "gobby-voice",
                "gobby-skills",
                "gobby-cron",
                "gobby-tests",
            ],
            "unlocked_tools": [],
        }

        # get_tool_schema for an internal server should be allowed immediately
        schema_event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "create_task"},
        )
        result = await engine.evaluate(schema_event, "test-session", variables)
        assert result.decision == "allow"

        # get_tool_schema for an external server should still be blocked
        ext_event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server_name": "context7", "tool_name": "get-library-docs"},
        )
        result = await engine.evaluate(ext_event, "test-session", variables)
        assert result.decision == "block"
