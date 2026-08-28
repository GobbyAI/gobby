"""Tests for plan-mode rules.

Verifies plan-mode detection, Consider guidance, mode_level tracking, and resets.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "22222222-2222-4222-8222-222222222222"


def _create_session(db: HubDatabase) -> None:
    db.execute(
        "INSERT INTO projects (id, name, created_at) "
        "VALUES (%s, %s, CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING",
        (PROJECT_ID, "plan-mode-test"),
    )
    db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (SESSION_ID, "plan-mode-ext", "21000000-0000-4000-8000-000000000002", "claude", PROJECT_ID),
    )


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _sync_bundled(db: HubDatabase) -> dict[str, Any]:
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    # Mark templates as installed so get_by_name() finds them
    db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return result


PLAN_MODE_RULES = {
    "handle-plan-mode-entry",
    "handle-plan-mode-exit",
    "reset-plan-consider-on-resolved-mode-exit",
    "reset-plan-mode-on-session-start",
    "teach-qwen-gcode-plan-mode",
}


class TestPlanModeSync:
    """Test that plan-mode rules sync correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All bundled plan-mode rules should sync to rule_definitions."""
        _sync_bundled(db)

        rules = manager.list_all()
        rule_names = {r.name for r in rules}

        assert PLAN_MODE_RULES.issubset(rule_names), f"Missing: {PLAN_MODE_RULES - rule_names}"

    def test_all_rules_have_group(self, db, manager) -> None:
        """All plan-mode rules should have group='plan-mode'."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in PLAN_MODE_RULES:
                body = row.definition_json
                assert body.get("group") == "plan-mode", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in PLAN_MODE_RULES:
                body = RuleDefinitionBody.model_validate(row.definition_json)
                for effect in body.resolved_effects:
                    assert effect.type in {
                        "block",
                        "inject_context",
                        "load_skill",
                        "set_variable",
                    }

    def test_inject_plan_skill_rule_deleted(self, db, manager) -> None:
        """inject-plan-skill (redundant duplicate) should not exist after sync."""
        _sync_bundled(db)

        rules = manager.list_all()
        rule_names = {r.name for r in rules}
        assert "inject-plan-skill" not in rule_names


class TestHandlePlanModeEntry:
    """Verify the interactive-only, one-shot Consider guidance."""

    def test_fires_on_turn_start_with_plan_mode_guard(self, db, manager) -> None:
        """Should fire on turn_start when plan_mode is set."""
        _sync_bundled(db)

        row = manager.get_by_name("handle-plan-mode-entry")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.agent_scope == ["default"]
        assert body.when is not None
        assert "plan_mode" in body.when
        assert "gobby_plan_consider_shown" in body.when
        assert "skill_loaded('plan')" not in body.when

    def test_effects_inject_consider_guidance_and_set_guard(self, db, manager) -> None:
        """Should inject adaptive planning guidance without loading plan."""
        _sync_bundled(db)

        row = manager.get_by_name("handle-plan-mode-entry")
        body = RuleDefinitionBody.model_validate(row.definition_json)

        effects = body.resolved_effects
        assert len(effects) == 2
        assert effects[0].type == "inject_context"
        guidance = effects[0].template or ""
        for phrase in (
            "Investigate the user's request and repository",
            "multiple dependent deliverables or subsystems",
            "public API, schema, migration, security, or destructive-risk work",
            "material unresolved product decisions",
            "multi-agent coordination or durable handoff requirements",
            "artifact, lifecycle automation, or adversarial review",
            "localized, low-risk work",
            "Strong signals determine whether Gobby planning is offered",
            "Always recommend **Lightweight** for bug fixes and maintenance",
            "regardless of breadth, risk, affected subsystems",
            "Recommend **Full** only for complex new features and complex refactors",
            "decision-complete plan artifact",
            "**Full:**",
            "**Lightweight:**",
        ):
            assert phrase in guidance
        assert all(effect.type != "load_skill" for effect in effects)
        assert effects[1].type == "set_variable"
        assert effects[1].variable == "gobby_plan_consider_shown"
        assert effects[1].value is True

    @pytest.mark.asyncio
    async def test_consider_guidance_fires_once_for_default_agent(self, db) -> None:
        _sync_bundled(db)
        variables = {"_agent_type": "default", "plan_mode": True}
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "plan the change"},
            metadata={"_platform_session_id": SESSION_ID},
        )
        engine = RuleEngine(db)

        first = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        second = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        assert first.context is not None
        assert "Investigate the user's request and repository" in first.context
        assert second.context is None or (
            "Investigate the user's request and repository" not in second.context
        )
        assert variables["gobby_plan_consider_shown"] is True

    @pytest.mark.asyncio
    async def test_first_plan_prompt_resolves_mode_before_consider_rule(self, db) -> None:
        _sync_bundled(db)
        _create_session(db)
        SessionVariableManager(db).merge_variables(
            SESSION_ID,
            {"_agent_type": "default"},
        )
        session = SimpleNamespace(
            session_type="terminal",
            context_usage_ratio=None,
            context_used_tokens=None,
            context_window=None,
        )
        session_manager = SimpleNamespace(get=lambda _session_id: session)
        handler = WorkflowHookHandler(
            rule_engine=RuleEngine(db),
            session_manager=cast(SessionManager, session_manager),
        )
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "first prompt", "permission_mode": "plan"},
            metadata={
                "_platform_session_id": SESSION_ID,
                "session_type": "terminal",
            },
        )

        response = await handler._evaluate_rules(event)

        variables = SessionVariableManager(db).get_variables(SESSION_ID)
        assert response.context is not None
        assert "Investigate the user's request and repository" in response.context
        assert variables["plan_mode"] is True
        assert variables["gobby_plan_consider_shown"] is True
        assert variables.get("plan_skill_loaded") is not True

        exit_event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "approved", "permission_mode": "normal"},
            metadata={
                "_platform_session_id": SESSION_ID,
                "session_type": "terminal",
            },
        )
        await handler._evaluate_rules(exit_event)
        exited_variables = SessionVariableManager(db).get_variables(SESSION_ID)
        assert exited_variables["plan_mode"] is False
        assert exited_variables["gobby_plan_consider_shown"] is False

        reentry = await handler._evaluate_rules(event)
        assert reentry.context is not None
        assert "Investigate the user's request and repository" in reentry.context

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_type", ["developer", "planner", "qa-reviewer"])
    async def test_spawned_agent_types_receive_no_consider_guidance(
        self,
        db,
        agent_type: str,
    ) -> None:
        _sync_bundled(db)
        variables = {"_agent_type": agent_type, "plan_mode": True}
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"prompt": "plan the change"},
            metadata={"_platform_session_id": SESSION_ID},
        )

        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables=variables,
        )

        assert response.context is None or (
            "Investigate the user's request and repository" not in response.context
        )
        assert "gobby_plan_consider_shown" not in variables


class TestTeachQwenGcodePlanMode:
    """Verify Qwen plan-mode gcode guidance."""

    def test_targets_qwen_plan_mode_once(self, db, manager) -> None:
        _sync_bundled(db)

        row = manager.get_by_name("teach-qwen-gcode-plan-mode")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.when is not None
        assert "plan_mode" in body.when
        assert "source == 'qwen'" in body.when
        assert "qwen_gcode_plan_hint_shown" in body.when

    def test_injects_gcode_context_and_sets_guard(self, db, manager) -> None:
        _sync_bundled(db)

        row = manager.get_by_name("teach-qwen-gcode-plan-mode")
        body = RuleDefinitionBody.model_validate(row.definition_json)

        effects = body.resolved_effects
        assert len(effects) == 2
        assert effects[0].type == "inject_context"
        assert effects[0].template is not None
        assert "gcode outline" in effects[0].template
        assert "gcode symbol <full-uuid>" in effects[0].template
        assert "search or outline results" in effects[0].template
        assert "write/destructive shell commands remain blocked" in effects[0].template
        assert effects[1].type == "set_variable"
        assert effects[1].variable == "qwen_gcode_plan_hint_shown"
        assert effects[1].value is True


class TestHandlePlanModeExit:
    """Verify handle-plan-mode-exit: after_tool on approved ExitPlanMode."""

    def test_fires_on_after_tool_for_exit_plan_mode(self, db, manager) -> None:
        """Should fire on after_tool when ExitPlanMode succeeds."""
        _sync_bundled(db)

        row = manager.get_by_name("handle-plan-mode-exit")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "after_tool"
        assert body.when is not None
        assert "ExitPlanMode" in body.when
        assert "is_failure" in body.when

    def test_clears_plan_mode_and_epoch_guards(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        """Should clear plan_mode and both Plan Mode epoch guards."""
        _sync_bundled(db)

        row = manager.get_by_name("handle-plan-mode-exit")
        body = RuleDefinitionBody.model_validate(row.definition_json)

        effects = body.resolved_effects
        effects_by_variable = {effect.variable: effect for effect in effects}
        assert len(effects) == 3
        assert effects_by_variable["plan_mode"].value is False
        assert effects_by_variable["qwen_gcode_plan_hint_shown"].value is False
        assert effects_by_variable["gobby_plan_consider_shown"].value is False

    def test_resolved_mode_exit_resets_consider_epoch(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        _sync_bundled(db)

        row = manager.get_by_name("reset-plan-consider-on-resolved-mode-exit")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.event.value == "turn_start"
        assert body.agent_scope == ["default"]
        assert body.when is not None
        assert "not variables.get('plan_mode')" in body.when
        assert "gobby_plan_consider_shown" in body.when
        effect = body.resolved_effects[0]
        assert effect.variable == "gobby_plan_consider_shown"
        assert effect.value is False


class TestResetPlanModeOnSessionStart:
    """Verify reset-plan-mode-on-session-start clears plan_mode."""

    def test_resets_plan_mode_on_session_start(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        """Should set plan_mode to false on session_start."""
        _sync_bundled(db)

        row = manager.get_by_name("reset-plan-mode-on-session-start")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "session_start"
        effects_by_variable = {effect.variable: effect for effect in body.resolved_effects}
        assert effects_by_variable["plan_mode"].value is False
        assert effects_by_variable["qwen_gcode_plan_hint_shown"].value is False
        assert effects_by_variable["gobby_plan_consider_shown"].value is False

    def test_when_condition_covers_clear_compact_startup(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        """Should fire on clear, compact, and startup sources."""
        _sync_bundled(db)

        row = manager.get_by_name("reset-plan-mode-on-session-start")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.when is not None
        assert "clear" in body.when
        assert "compact" in body.when
        assert "startup" in body.when
