"""Tests for progressive-discovery rules.

Verifies:
- Hardcoded auto-discover seeds servers_listed on BEFORE_AGENT
- Block rules enforce list_tools -> get_tool_schema -> call_tool
- Tracker rules record state on after_tool events
- Reset rule clears state on context loss

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
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

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
    from gobby.workflows.sync_rules import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    # Mark templates as installed so get_by_name() finds them
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")
    return result


PROGRESSIVE_DISCOVERY_RULES = {
    "require-server-listed-for-schema",
    "require-schema-before-call",
    "track-schema-lookup",
    "track-servers-listed",
    "track-listed-servers",
    "reset-progressive-discovery",
}


class TestProgressiveDiscoverySync:
    """Test that progressive-discovery rules sync correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All progressive-discovery rules should sync to workflow_definitions."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        rule_names = {r.name for r in rules}

        assert PROGRESSIVE_DISCOVERY_RULES.issubset(rule_names), (
            f"Missing: {PROGRESSIVE_DISCOVERY_RULES - rule_names}"
        )

    def test_require_servers_listed_removed(self, db, manager) -> None:
        """require-servers-listed was removed (replaced by hardcoded auto-discover)."""
        _sync_bundled(db)

        row = manager.get_by_name("require-servers-listed")
        assert row is None

    def test_all_rules_have_progressive_discovery_tag(self, db, manager) -> None:
        """All rules should be tagged with 'progressive-discovery'."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in PROGRESSIVE_DISCOVERY_RULES:
                assert row.tags and "progressive-discovery" in row.tags, (
                    f"{row.name} missing 'progressive-discovery' tag"
                )

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        valid_types = {"block", "set_variable", "inject_context", "mcp_call"}
        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in PROGRESSIVE_DISCOVERY_RULES:
                body = RuleDefinitionBody.model_validate_json(row.definition_json)
                for effect in body.resolved_effects:
                    assert effect.type in valid_types


class TestRequireServerListedForSchema:
    """Verify require-server-listed-for-schema blocks get_tool_schema."""

    def test_has_block_effect(self, db, manager) -> None:
        """Should have a block effect (not mcp_call auto-heal)."""
        _sync_bundled(db)

        row = manager.get_by_name("require-server-listed-for-schema")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        effects = body.resolved_effects
        block_effects = [e for e in effects if e.type == "block"]
        assert len(block_effects) == 1
        assert "list_tools" in block_effects[0].reason

    def test_when_checks_is_server_listed(self, db, manager) -> None:
        """Should use is_server_listed helper and check tool name."""
        _sync_bundled(db)

        row = manager.get_by_name("require-server-listed-for-schema")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "is_server_listed" in body.when
        assert "get_tool_schema" in body.when


class TestRequireSchemaBeforeCall:
    """Verify require-schema-before-call blocks call_tool without schema."""

    def test_has_block_effect(self, db, manager) -> None:
        """Should have a block effect (not mcp_call auto-heal)."""
        _sync_bundled(db)

        row = manager.get_by_name("require-schema-before-call")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        effects = body.resolved_effects
        block_effects = [e for e in effects if e.type == "block"]
        assert len(block_effects) == 1
        assert "get_tool_schema" in block_effects[0].reason

    def test_when_checks_tool_unlocked(self, db, manager) -> None:
        """Should check tool exemptions, unlock state, and call_tool."""
        _sync_bundled(db)

        row = manager.get_by_name("require-schema-before-call")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "is_tool_unlocked" in body.when
        assert "is_discovery_tool" in body.when
        assert "is_operator_tool" in body.when
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
        assert body.effects[0].type == "set_variable"
        assert body.effects[0].variable == "unlocked_tools"

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
        assert body.effects[0].type == "set_variable"
        assert body.effects[0].variable == "servers_listed"
        assert body.effects[0].value is True

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
        assert body.effects[0].type == "set_variable"
        assert body.effects[0].variable == "listed_servers"

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


class TestPreseedRemoved:
    """Verify preseed-progressive-discovery has been removed."""

    def test_preseed_rule_does_not_exist(self, db, manager) -> None:
        """Preseed rule should not exist in workflow_definitions."""
        _sync_bundled(db)

        row = manager.get_by_name("preseed-progressive-discovery")
        assert row is None


class TestRuleDefinitionBodyToolsField:
    """Verify tools field on RuleDefinitionBody works as pre-filter."""

    def test_tools_field_accepted(self) -> None:
        """RuleDefinitionBody should accept a tools field."""
        from gobby.workflows.definitions import RuleEffect, RuleEvent

        body = RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            tools=["mcp__gobby__list_tools"],
            effects=[RuleEffect(type="block", reason="test")],
        )
        assert body.tools == ["mcp__gobby__list_tools"]

    def test_tools_field_none_by_default(self) -> None:
        """RuleDefinitionBody.tools should default to None."""
        from gobby.workflows.definitions import RuleEffect, RuleEvent

        body = RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            effects=[RuleEffect(type="block", reason="test")],
        )
        assert body.tools is None

    def test_enforcement_rules_use_block_effects(self, db, manager) -> None:
        """Enforcement rules should use block effects, not mcp_call auto-heal."""
        _sync_bundled(db)

        for rule_name in ["require-server-listed-for-schema", "require-schema-before-call"]:
            row = manager.get_by_name(rule_name)
            assert row is not None, f"{rule_name} not found"
            body = RuleDefinitionBody.model_validate_json(row.definition_json)
            block_effects = [e for e in body.resolved_effects if e.type == "block"]
            assert len(block_effects) == 1, f"{rule_name} should have exactly 1 block effect"
            mcp_effects = [e for e in body.resolved_effects if e.type == "mcp_call"]
            assert len(mcp_effects) == 0, f"{rule_name} should not have mcp_call effects"


class TestPriorityOrdering:
    """Verify priority ordering within progressive-discovery group."""

    def test_enforcement_rules_ordered_correctly(self, db, manager) -> None:
        """require-server-listed-for-schema should fire before require-schema-before-call."""
        _sync_bundled(db)

        schema_rule = manager.get_by_name("require-server-listed-for-schema")
        call_rule = manager.get_by_name("require-schema-before-call")

        assert schema_rule is not None
        assert call_rule is not None
        assert schema_rule.priority <= call_rule.priority


def _make_hook_event(
    event_type: HookEventType,
    tool_name: str = "",
    tool_input: dict | None = None,
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    """Create a HookEvent for testing."""
    data = {"tool_name": tool_name}
    if tool_input is not None:
        data["tool_input"] = tool_input
    return HookEvent(
        event_type=event_type,
        session_id="test-session-ext",
        source=source,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": "test-session"},
    )


class TestRuleEngineIntegration:
    """End-to-end tests: RuleEngine.evaluate() with progressive discovery rules.

    Tests the actual condition evaluation path including is_server_listed,
    is_tool_unlocked, and is_discovery_tool. The enforcement rules now block
    instead of auto-healing.
    """

    @pytest.fixture
    def engine(self, db) -> RuleEngine:
        _sync_bundled(db)
        # Disable all rules first, then enable only the progressive discovery rules
        db.execute("UPDATE workflow_definitions SET enabled = 0")
        for name in PROGRESSIVE_DISCOVERY_RULES:
            db.execute(
                "UPDATE workflow_definitions SET enabled = 1 WHERE name = ?",
                (name,),
            )
        return RuleEngine(db)

    @pytest.mark.asyncio
    async def test_hardcoded_auto_discover_on_before_agent(self, engine) -> None:
        """BEFORE_AGENT should emit auto-discover mcp_call when servers_listed is false."""
        variables: dict = {}
        event = _make_hook_event(HookEventType.BEFORE_AGENT)
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "allow"
        mcp_calls = (result.metadata or {}).get("mcp_calls", [])
        assert len(mcp_calls) == 1
        assert mcp_calls[0]["tool"] == "list_mcp_servers"
        assert mcp_calls[0]["arguments"]["name_filter"] == "gobby-*"
        assert mcp_calls[0]["inject_result"] is True
        assert variables["servers_listed"] is True

    @pytest.mark.asyncio
    async def test_auto_discover_skips_when_already_listed(self, engine) -> None:
        """BEFORE_AGENT should not emit auto-discover when servers_listed is true."""
        variables: dict = {"servers_listed": True}
        event = _make_hook_event(HookEventType.BEFORE_AGENT)
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "allow"
        mcp_calls = (result.metadata or {}).get("mcp_calls", [])
        assert len(mcp_calls) == 0

    @pytest.mark.asyncio
    async def test_get_tool_schema_blocked_when_server_not_listed(self, engine) -> None:
        """get_tool_schema should be blocked when list_tools not called for server."""
        variables = {"enforce_tool_schema_check": True, "listed_servers": []}
        event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "create_task"},
        )
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "block"
        assert "list_tools" in result.reason

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
    async def test_get_tool_schema_allowed_for_pipeline_source(self, engine) -> None:
        """Pipeline-sourced get_tool_schema should bypass discovery-order enforcement."""
        variables = {"enforce_tool_schema_check": True, "listed_servers": []}
        event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "create_task"},
            source=SessionSource.PIPELINE,
        )
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "allow"

    @pytest.mark.asyncio
    async def test_call_tool_blocked_when_schema_missing(self, engine) -> None:
        """call_tool should be blocked when get_tool_schema not called for tool."""
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
        assert result.decision == "block"
        assert "get_tool_schema" in result.reason

    @pytest.mark.asyncio
    async def test_call_tool_allowed_for_send_keys_without_schema_lookup(self, engine) -> None:
        """Operator tool send_keys should bypass schema-unlock gating."""
        variables = {
            "enforce_tool_schema_check": True,
            "unlocked_tools": [],
        }
        event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-sessions",
                "tool_name": "send_keys",
                "arguments": {"keys": "pwd\n"},
            },
        )
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "allow"

    @pytest.mark.asyncio
    async def test_call_tool_allowed_for_capture_output_without_schema_lookup(self, engine) -> None:
        """Operator tool capture_output should bypass schema-unlock gating."""
        variables = {
            "enforce_tool_schema_check": True,
            "unlocked_tools": [],
        }
        event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-sessions",
                "tool_name": "capture_output",
                "arguments": {},
            },
        )
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "allow"

    @pytest.mark.parametrize("mcp_tool", ["create_task", "add_label", "update_task"])
    @pytest.mark.asyncio
    async def test_call_tool_allowed_after_schema_lookup(self, engine, mcp_tool: str) -> None:
        """call_tool should be allowed after get_tool_schema was called."""
        variables = {
            "enforce_tool_schema_check": True,
            "unlocked_tools": [f"gobby-tasks:{mcp_tool}"],
        }
        event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": mcp_tool,
                "arguments": {"title": "test"},
            },
        )
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "allow"

    @pytest.mark.asyncio
    async def test_call_tool_allowed_for_pipeline_source(self, engine) -> None:
        """Pipeline-sourced call_tool should bypass schema lookup enforcement."""
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
            source=SessionSource.PIPELINE,
        )
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "allow"

    @pytest.mark.asyncio
    async def test_call_tool_allowed_when_enforce_flag_false(self, engine) -> None:
        """call_tool should be allowed when enforce_tool_schema_check=False.

        Guards #12135: pipeline sessions seed this flag to False to bypass the
        rule independent of event-time source resolution, which races against
        the just-written pipeline session row.  Uses source=CODEX here to
        simulate the exact failure mode (source resolution defaulted to CODEX
        because the session row was not yet visible).
        """
        variables = {
            "enforce_tool_schema_check": False,
            "unlocked_tools": [],
        }
        event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks-ops",
                "tool_name": "start_expansion_run",
                "arguments": {"task_id": "#123"},
            },
            source=SessionSource.CODEX,
        )
        result = await engine.evaluate(event, "test-session", variables)
        assert result.decision == "allow"

    @pytest.mark.asyncio
    async def test_get_tool_schema_allowed_when_enforce_flag_false(self, engine) -> None:
        """get_tool_schema should also be allowed when enforce_tool_schema_check=False.

        Sibling of the call_tool case above — same session-variable short-circuit
        covers require-server-listed-for-schema too.
        """
        variables = {
            "enforce_tool_schema_check": False,
            "listed_servers": [],
        }
        event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server_name": "gobby-tasks-ops", "tool_name": "get_expansion_run"},
            source=SessionSource.CODEX,
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

    @pytest.mark.parametrize("mcp_tool", ["create_task", "add_label", "update_task"])
    @pytest.mark.asyncio
    async def test_tracking_rules_set_variables_via_after_tool(self, engine, mcp_tool: str) -> None:
        """Tracking rules should set variables when after_tool events fire."""
        variables: dict = {
            "enforce_tool_schema_check": True,
        }

        # Step 1: Fire after_tool for list_mcp_servers
        after_list_servers = _make_hook_event(
            HookEventType.AFTER_TOOL,
            tool_name="mcp__gobby__list_mcp_servers",
        )
        result = await engine.evaluate(after_list_servers, "test-session", variables)
        assert result.decision == "allow"
        assert variables.get("servers_listed") is True

        # Step 2: Fire after_tool for list_tools
        after_list_tools = _make_hook_event(
            HookEventType.AFTER_TOOL,
            tool_name="mcp__gobby__list_tools",
            tool_input={"server_name": "gobby-tasks"},
        )
        result = await engine.evaluate(after_list_tools, "test-session", variables)
        assert result.decision == "allow"
        assert "gobby-tasks" in variables.get("listed_servers", [])

        # Step 3: Fire after_tool for get_tool_schema
        after_schema = _make_hook_event(
            HookEventType.AFTER_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": mcp_tool},
        )
        result = await engine.evaluate(after_schema, "test-session", variables)
        assert result.decision == "allow"
        assert f"gobby-tasks:{mcp_tool}" in variables.get("unlocked_tools", [])

    @pytest.mark.asyncio
    async def test_tracking_schema_lookup_uses_server_and_tool_aliases(self, engine) -> None:
        """track-schema-lookup should accept the same aliases as is_tool_unlocked."""
        variables: dict = {
            "enforce_tool_schema_check": True,
        }

        after_schema = _make_hook_event(
            HookEventType.AFTER_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server": "gobby-tasks", "tool": "add_label"},
        )
        result = await engine.evaluate(after_schema, "test-session", variables)
        assert result.decision == "allow"
        assert "gobby-tasks:add_label" in variables.get("unlocked_tools", [])

    @pytest.mark.asyncio
    async def test_full_discovery_flow_with_blocking(self, engine) -> None:
        """Full flow: block → track → allow progression.

        1. get_tool_schema blocked (server not listed)
        2. After list_tools tracking, get_tool_schema allowed
        3. call_tool blocked (schema not looked up)
        4. After get_tool_schema tracking, call_tool allowed
        """
        variables: dict = {
            "enforce_tool_schema_check": True,
        }

        # Step 1: get_tool_schema blocked (server not listed)
        schema_event = _make_hook_event(
            HookEventType.BEFORE_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "create_task"},
        )
        result = await engine.evaluate(schema_event, "test-session", variables)
        assert result.decision == "block"

        # Step 2: Simulate list_tools completing (tracking rule fires)
        after_list_tools = _make_hook_event(
            HookEventType.AFTER_TOOL,
            tool_name="mcp__gobby__list_tools",
            tool_input={"server_name": "gobby-tasks"},
        )
        await engine.evaluate(after_list_tools, "test-session", variables)
        assert "gobby-tasks" in variables.get("listed_servers", [])

        # Step 3: get_tool_schema now allowed (server listed)
        result = await engine.evaluate(schema_event, "test-session", variables)
        assert result.decision == "allow"

        # Step 4: call_tool blocked (schema not looked up)
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
        assert result.decision == "block"

        # Step 5: Simulate get_tool_schema completing (tracking rule fires)
        after_schema = _make_hook_event(
            HookEventType.AFTER_TOOL,
            tool_name="mcp__gobby__get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "create_task"},
        )
        await engine.evaluate(after_schema, "test-session", variables)
        assert "gobby-tasks:create_task" in variables.get("unlocked_tools", [])

        # Step 6: call_tool now allowed (schema was looked up)
        result = await engine.evaluate(call_event, "test-session", variables)
        assert result.decision == "allow"
