"""Tests for plan-mode rules.

Verifies plan-mode detection (enter/exit via observer + rules),
skill loading directives, mode_level tracking, and session_start reset.
"""

from __future__ import annotations

import json

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _sync_bundled(db):
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    # Mark templates as installed so get_by_name() finds them
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")
    return result


PLAN_MODE_RULES = {
    "block-writes-outside-plan-artifact",
    "handle-plan-mode-entry",
    "handle-plan-mode-exit",
    "reset-plan-mode-on-session-start",
    "teach-gemini-qwen-gcode-plan-mode",
}


class TestPlanModeSync:
    """Test that plan-mode rules sync correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All 3 plan-mode rules should sync to workflow_definitions."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        rule_names = {r.name for r in rules}

        assert PLAN_MODE_RULES.issubset(rule_names), f"Missing: {PLAN_MODE_RULES - rule_names}"

    def test_all_rules_have_group(self, db, manager) -> None:
        """All plan-mode rules should have group='plan-mode'."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in PLAN_MODE_RULES:
                body = json.loads(row.definition_json)
                assert body.get("group") == "plan-mode", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in PLAN_MODE_RULES:
                body = RuleDefinitionBody.model_validate_json(row.definition_json)
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

        rules = manager.list_all(workflow_type="rule")
        rule_names = {r.name for r in rules}
        assert "inject-plan-skill" not in rule_names


class TestHandlePlanModeEntry:
    """Verify handle-plan-mode-entry: turn_start, skill directive only."""

    def test_fires_on_turn_start_with_plan_mode_guard(self, db, manager) -> None:
        """Should fire on turn_start when plan_mode is set."""
        _sync_bundled(db)

        row = manager.get_by_name("handle-plan-mode-entry")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.when is not None
        assert "plan_mode" in body.when
        assert "skill_loaded('plan')" in body.when

    def test_effects_load_skill_without_setting_guard(self, db, manager) -> None:
        """Should emit plan skill directive without marking it loaded."""
        _sync_bundled(db)

        row = manager.get_by_name("handle-plan-mode-entry")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        effects = body.resolved_effects
        assert len(effects) == 1
        assert effects[0].type == "load_skill"
        assert effects[0].skill == "plan"


class TestTeachGeminiQwenGcodePlanMode:
    """Verify Gemini/Qwen plan-mode gcode guidance."""

    def test_targets_gemini_and_qwen_plan_mode_once(self, db, manager) -> None:
        _sync_bundled(db)

        row = manager.get_by_name("teach-gemini-qwen-gcode-plan-mode")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.when is not None
        assert "plan_mode" in body.when
        assert "source in ('gemini', 'qwen')" in body.when
        assert "gemini_qwen_gcode_plan_hint_shown" in body.when

    def test_injects_gcode_context_and_sets_guard(self, db, manager) -> None:
        _sync_bundled(db)

        row = manager.get_by_name("teach-gemini-qwen-gcode-plan-mode")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        effects = body.resolved_effects
        assert len(effects) == 2
        assert effects[0].type == "inject_context"
        assert effects[0].template is not None
        assert "gcode outline" in effects[0].template
        assert "gcode symbol <full-uuid>" in effects[0].template
        assert "search or outline results" in effects[0].template
        assert "write/destructive shell commands remain blocked" in effects[0].template
        assert effects[1].type == "set_variable"
        assert effects[1].variable == "gemini_qwen_gcode_plan_hint_shown"
        assert effects[1].value is True


class TestHandlePlanModeExit:
    """Verify handle-plan-mode-exit: after_tool on approved ExitPlanMode."""

    def test_fires_on_after_tool_for_exit_plan_mode(self, db, manager) -> None:
        """Should fire on after_tool when ExitPlanMode succeeds."""
        _sync_bundled(db)

        row = manager.get_by_name("handle-plan-mode-exit")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "after_tool"
        assert body.when is not None
        assert "ExitPlanMode" in body.when
        assert "is_failure" in body.when

    def test_clears_plan_mode_and_gemini_qwen_hint_guard(self, db, manager) -> None:
        """Should clear plan_mode and the Gemini/Qwen gcode hint guard."""
        _sync_bundled(db)

        row = manager.get_by_name("handle-plan-mode-exit")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        effects = body.resolved_effects
        assert len(effects) == 2
        assert effects[0].variable == "plan_mode"
        assert effects[0].value is False
        assert effects[1].variable == "gemini_qwen_gcode_plan_hint_shown"
        assert effects[1].value is False


class TestResetPlanModeOnSessionStart:
    """Verify reset-plan-mode-on-session-start clears plan_mode."""

    def test_resets_plan_mode_on_session_start(self, db, manager) -> None:
        """Should set plan_mode to false on session_start."""
        _sync_bundled(db)

        row = manager.get_by_name("reset-plan-mode-on-session-start")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "session_start"
        effects_by_variable = {effect.variable: effect for effect in body.effects}
        assert effects_by_variable["plan_mode"].value is False
        assert effects_by_variable["gemini_qwen_gcode_plan_hint_shown"].value is False

    def test_when_condition_covers_clear_compact_startup(self, db, manager) -> None:
        """Should fire on clear, compact, and startup sources."""
        _sync_bundled(db)

        row = manager.get_by_name("reset-plan-mode-on-session-start")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "clear" in body.when
        assert "compact" in body.when
        assert "startup" in body.when
