"""Tests for skill-discovery rules.

Verifies require-python-skill, require-rust-skill, and reset-skill-injection
rules sync correctly, have valid structure, and evaluate conditions properly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.safe_evaluator import (
    ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS,
    SafeExpressionEvaluator,
    build_condition_helpers,
)
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
    """Sync bundled rules and coerce source to 'installed' for test evaluation.

    sync_bundled_rules() imports templates with source='template', but the rule
    engine (list_rules_by_event) filters out template-sourced rules. Tests need
    rules to evaluate, so we coerce source to 'installed' to simulate activation
    via install_from_template().
    """
    from gobby.workflows.sync_rules import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")
    return result


SKILL_DISCOVERY_RULES = {
    "discover-skill-hubs-on-turn-start",
    "require-python-skill",
    "require-rust-skill",
    "reset-skill-injection",
}

REPLACED_SKILL_RULES = {
    "inject-python-skill",
    "inject-rust-skill",
    "block-and-teach-code-index",
}

BREVITY_RULES = {
    "opt-out-brevity",
    "load-brevity-on-turn-start",
    "inject-brevity-drift-feedback",
    "remind-brevity-on-turn-start",
    "detect-brevity-literal-drift",
    "detect-brevity-contrastive-drift",
}


# --- Sync tests ---


class TestSkillDiscoverySync:
    """Test that skill-discovery rules sync correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All skill-discovery rules should sync to workflow_definitions."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        rule_names = {r.name for r in rules}

        assert SKILL_DISCOVERY_RULES.issubset(rule_names), (
            f"Missing: {SKILL_DISCOVERY_RULES - rule_names}"
        )
        assert REPLACED_SKILL_RULES.isdisjoint(rule_names)

    def test_all_rules_have_group(self, db, manager) -> None:
        """All rules should have group='skill-discovery'."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in SKILL_DISCOVERY_RULES:
                body = json.loads(row.definition_json)
                assert body.get("group") == "skill-discovery", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in SKILL_DISCOVERY_RULES:
                body = RuleDefinitionBody.model_validate_json(row.definition_json)
                assert body.event is not None
                assert body.effects


# --- discover-skill-hubs-on-turn-start ---


class TestDiscoverSkillHubsOnTurnStart:
    """Verify the once-per-session skill hub discovery rule."""

    def test_structure(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("discover-skill-hubs-on-turn-start")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.event.value == "turn_start"
        assert body.when == "not variables.get('skill_discovery_instructions_shown')"
        assert [effect.type for effect in body.effects] == [
            "load_skill",
            "mcp_call",
            "set_variable",
        ]
        assert body.effects[0].skill == "loading-skills"
        assert body.effects[1].server == "gobby-skills"
        assert body.effects[1].tool == "list_hubs"
        assert body.effects[1].inject_result is True
        assert body.effects[2].variable == "skill_discovery_instructions_shown"
        assert body.effects[2].value is True

    @pytest.mark.asyncio
    async def test_injects_guidance_and_sets_guard_after_success(self, db) -> None:
        _sync_bundled(db)

        async def dispatcher(server: str, tool: str, args: dict, event: Any) -> dict[str, Any]:
            assert server == "gobby-skills"
            assert tool == "list_hubs"
            assert args == {}
            return {
                "success": True,
                "result": {
                    "success": True,
                    "hubs": [
                        {
                            "name": "clawdhub",
                            "type": "clawdhub",
                            "auth_required": False,
                            "auth_configured": True,
                        }
                    ],
                },
            }

        variables: dict[str, Any] = {"loaded_skills": ["brevity"], "servers_listed": True}
        engine = RuleEngine(db, mcp_dispatcher=dispatcher)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id="test-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "hi"},
        )

        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.context is not None
        assert skill_fetch_directive("loading-skills") in response.context
        assert "<available-skill-hubs>" in response.context
        assert "- clawdhub (clawdhub, auth: not required)" in response.context
        assert variables["skill_discovery_instructions_shown"] is True
        assert all(
            call.get("tool") != "list_hubs" for call in response.metadata.get("mcp_calls", [])
        )

    @pytest.mark.asyncio
    async def test_does_not_set_guard_when_hub_listing_fails(self, db) -> None:
        _sync_bundled(db)

        async def dispatcher(server: str, tool: str, args: dict, event: Any) -> dict[str, Any]:
            return {"success": False, "result": {"error": "hub manager unavailable"}}

        variables: dict[str, Any] = {"loaded_skills": ["brevity"], "servers_listed": True}
        engine = RuleEngine(db, mcp_dispatcher=dispatcher)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id="test-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "hi"},
        )

        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables.get("skill_discovery_instructions_shown") is None


# --- brevity rules ---


class TestBrevityRules:
    def _turn_variables(self, *, loaded: bool = True, disabled: bool = False) -> dict[str, Any]:
        return {
            "loaded_skills": ["brevity"] if loaded else [],
            "brevity_disabled": disabled,
            "skill_discovery_instructions_shown": True,
            "memory_nudge_fired": True,
            "servers_listed": True,
        }

    def test_brevity_rules_sync_and_old_first_turn_rule_is_orphaned(self, db, manager) -> None:
        _sync_bundled(db)

        rule_names = {r.name for r in manager.list_all(workflow_type="rule")}

        assert BREVITY_RULES.issubset(rule_names)
        assert "inject-brevity-on-first-turn" not in rule_names

    def test_reinforce_brevity_structure(self, db, manager) -> None:
        _sync_bundled(db)

        load_row = manager.get_by_name("load-brevity-on-turn-start")
        assert load_row is not None
        load_body = RuleDefinitionBody.model_validate_json(load_row.definition_json)
        assert load_body.event.value == "turn_start"
        assert load_body.effects[0].type == "load_skill"
        assert load_body.effects[0].skill == "brevity"
        assert load_body.when == (
            "not variables.get('brevity_disabled') and not skill_loaded('brevity')"
        )

        reminder_row = manager.get_by_name("remind-brevity-on-turn-start")
        assert reminder_row is not None
        reminder_body = RuleDefinitionBody.model_validate_json(reminder_row.definition_json)
        assert reminder_body.event.value == "turn_start"
        assert reminder_body.effects[0].type == "inject_context"

    def test_detect_brevity_contrastive_rule_uses_allowed_regex_patterns(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("detect-brevity-contrastive-drift")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        definition = body.when or ""
        for pattern in ASSISTANT_RESPONSE_CONTRASTIVE_PATTERNS:
            assert pattern in definition

    def test_brevity_session_variables_are_defined(self) -> None:
        import yaml

        from gobby.workflows.sync_rules import get_bundled_rules_path

        vars_path = get_bundled_rules_path().parent / "variables" / "gobby-default-variables.yaml"
        variables = yaml.safe_load(vars_path.read_text())["variables"]

        assert variables["brevity_disabled"]["value"] is False
        assert variables["brevity_last_violation"]["value"] == ""
        assert variables["brevity_last_violation_rule"]["value"] == ""
        assert variables["code_index_navigation_used_this_turn"]["value"] is False

    @pytest.mark.asyncio
    async def test_reinforcer_repeats_after_brevity_is_loaded(self, db) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = self._turn_variables(loaded=True)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id="test-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "fix this"},
        )

        first = await engine.evaluate(event, session_id="sess-1", variables=variables)
        second = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert first.context is not None
        assert second.context is not None
        assert "Brevity reminder: answer first; keep context tight." in first.context
        assert "Brevity reminder: answer first; keep context tight." in second.context

    @pytest.mark.asyncio
    async def test_opt_out_prompt_disables_and_suppresses_brevity_rules(self, db) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = self._turn_variables(loaded=False)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id="test-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": " Normal Mode "},
        )

        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert variables["brevity_disabled"] is True
        assert response.context is None or "brevity" not in response.context.lower()

    @pytest.mark.asyncio
    async def test_brevity_drift_persists_then_clears_on_next_turn(self, db) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = self._turn_variables(loaded=True)
        turn_end = HookEvent(
            event_type=HookEventType.AFTER_AGENT,
            session_id="test-session",
            source=SessionSource.GEMINI,
            timestamp=datetime.now(UTC),
            data={"response": "In summary, the fix is applied."},
        )

        await engine.evaluate(turn_end, session_id="sess-1", variables=variables)

        assert variables["brevity_last_violation"] == "In summary"
        assert variables["brevity_last_violation_rule"] == "banned literal phrase"

        turn_start = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id="test-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "next"},
        )

        response = await engine.evaluate(turn_start, session_id="sess-1", variables=variables)

        assert response.context is not None
        assert "Brevity drift detected last turn" in response.context
        assert "`In summary`" in response.context
        assert variables["brevity_last_violation"] == ""
        assert variables["brevity_last_violation_rule"] == ""

    @pytest.mark.asyncio
    async def test_opt_out_suppresses_drift_detection(self, db) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = self._turn_variables(loaded=True, disabled=True)
        event = HookEvent(
            event_type=HookEventType.AFTER_AGENT,
            session_id="test-session",
            source=SessionSource.GEMINI,
            timestamp=datetime.now(UTC),
            data={"response": "In summary, the fix is applied."},
        )

        await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert "brevity_last_violation" not in variables


# --- require-python-skill structure ---


class TestRequirePythonSkillStructure:
    """Verify require-python-skill rule structure."""

    def test_is_before_tool_event(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-python-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('python')" in body.when

    def test_has_block_effect_with_canonical_directive(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-python-skill")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == skill_fetch_directive("python")


# --- require-python-skill condition evaluation ---


class TestRequirePythonSkillCondition:
    """Test the require-python-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('python') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and event.data.get('canonical_file_path', '').endswith('.py')"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    def test_matches_python_write(self) -> None:
        assert self._eval("/project/src/main.py") is True

    def test_matches_python_edit(self) -> None:
        assert self._eval("/project/src/gobby/deep/module.py") is True

    def test_skips_non_python_file(self) -> None:
        assert self._eval("/project/config.yaml") is False

    def test_skips_rust_file(self) -> None:
        assert self._eval("/project/src/main.rs") is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/src/main.py", loaded_skills=["python"]) is False

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/main.py", injected_skills=["python"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/src/main.py", canonical_tool_kind="read") is False

    def test_skips_other_tool(self) -> None:
        assert self._eval("/project/src/main.py", canonical_tool_kind="execute") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


# --- require-rust-skill structure ---


class TestRequireRustSkillStructure:
    """Verify require-rust-skill rule structure."""

    def test_is_before_tool_event(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-rust-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "not skill_loaded('rust')" in body.when

    def test_has_block_effect_with_canonical_directive(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-rust-skill")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == skill_fetch_directive("rust")


# --- require-rust-skill condition evaluation ---


class TestRequireRustSkillCondition:
    """Test the require-rust-skill condition evaluates correctly."""

    CONDITION = (
        "not skill_loaded('rust') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and event.data.get('canonical_file_path', '').endswith('.rs')"
    )

    def _eval(
        self,
        file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
    ) -> bool:
        variables = {"loaded_skills": loaded_skills or []}
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    def test_matches_rust_write(self) -> None:
        assert self._eval("/project/src/main.rs") is True

    def test_matches_rust_edit(self) -> None:
        assert self._eval("/project/src/deep/lib.rs") is True

    def test_skips_non_rust_file(self) -> None:
        assert self._eval("/project/config.yaml") is False

    def test_skips_python_file(self) -> None:
        assert self._eval("/project/src/main.py") is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/src/main.rs", loaded_skills=["rust"]) is False

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/main.rs", injected_skills=["rust"]) is True

    def test_skips_non_edit_write_tool(self) -> None:
        assert self._eval("/project/src/main.rs", canonical_tool_kind="read") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False


class TestCodeIndexRuleCondition:
    """Test the code-index onboarding rule against canonical tool metadata."""

    CONDITION = (
        "variables.get('code_index_available') "
        "and not skill_loaded('code-index') "
        "and not variables.get('code_index_preflight_warning') "
        "and not event.data.get('canonical_code_index_navigation') "
        "and event.data.get('canonical_code_navigation_broad')"
    )

    def _eval(
        self,
        *,
        code_index_available: bool = True,
        canonical_code_navigation_broad: bool = True,
        canonical_code_index_navigation: bool = False,
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
        code_index_preflight_warning: bool = False,
    ) -> bool:
        variables = {
            "loaded_skills": loaded_skills or [],
            "code_index_available": code_index_available,
        }
        if code_index_preflight_warning:
            variables["code_index_preflight_warning"] = {
                "preflight": "code_index",
                "message": "gcode_index_unavailable",
            }
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_code_navigation_broad": canonical_code_navigation_broad,
                    "canonical_code_index_navigation": canonical_code_index_navigation,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    def test_matches_broad_code_navigation(self) -> None:
        assert self._eval(canonical_code_navigation_broad=True) is True

    def test_matches_search(self) -> None:
        assert self._eval(canonical_code_navigation_broad=True) is True

    def test_skips_narrow_context_read(self) -> None:
        assert self._eval(canonical_code_navigation_broad=False) is False

    def test_skips_when_code_index_unavailable(self) -> None:
        assert self._eval(code_index_available=False) is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval(loaded_skills=["code-index"]) is False

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval(injected_skills=["code-index"]) is True

    def test_skips_when_isolated_code_index_preflight_failed(self) -> None:
        assert self._eval(code_index_preflight_warning=True) is False

    def test_skips_gcode_navigation(self) -> None:
        assert self._eval(canonical_code_index_navigation=True) is False


class TestRequireCodeIndexSkillStructure:
    """Verify require-code-index-skill blocks with the canonical directive."""

    def test_has_block_effect_with_canonical_directive(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-code-index-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.event.value == "before_tool"
        assert body.when is not None
        assert "variables.get('code_index_available')" in body.when
        assert "not skill_loaded('code-index')" in body.when
        assert "not variables.get('code_index_preflight_warning')" in body.when
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert skill_fetch_directive("code-index") in body.effects[0].reason
        assert 'get_skill(name="code-index")' in body.effects[0].reason
        assert 'list_tools("gobby-skills")' in body.effects[0].reason

    def test_code_index_navigation_rules_sync(self, db, manager) -> None:
        _sync_bundled(db)
        expected = {
            "reset-code-index-navigation",
            "track-code-index-navigation",
            "prefer-gcode-for-code-search",
            "prefer-gcode-for-source-read",
        }
        rules = {row.name for row in manager.list_all(workflow_type="rule")}
        assert expected.issubset(rules)


class TestCodeIndexNavigationRules:
    """Verify indexed-project gcode-first rule behavior."""

    @staticmethod
    def _variables(*, loaded: bool = True, used: bool = False) -> dict[str, Any]:
        return {
            "loaded_skills": ["code-index"] if loaded else [],
            "code_index_available": True,
            "code_index_navigation_used_this_turn": used,
            "brevity_disabled": True,
            "skill_discovery_instructions_shown": True,
        }

    @staticmethod
    def _event(event_type: HookEventType, data: dict[str, Any]) -> HookEvent:
        return HookEvent(
            event_type=event_type,
            session_id="test-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data=data,
        )

    @pytest.mark.asyncio
    async def test_first_rg_requires_code_index_skill(self, db) -> None:
        _sync_bundled(db)
        variables = self._variables(loaded=False)
        event = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Bash",
                "command": "rg pattern src",
                "canonical_tool_kind": "search",
                "canonical_code_navigation_action": "search",
                "canonical_code_navigation_broad": True,
            },
        )

        response = await RuleEngine(db).evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"
        assert response.reason is not None
        assert skill_fetch_directive("code-index") in response.reason
        assert 'get_skill(name="code-index")' in response.reason

    @pytest.mark.asyncio
    async def test_loaded_code_index_blocks_rg_with_gcode_grep_guidance(self, db) -> None:
        _sync_bundled(db)
        event = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Bash",
                "command": "rg pattern src",
                "canonical_tool_kind": "search",
                "canonical_code_navigation_action": "search",
                "canonical_code_navigation_broad": True,
            },
        )

        response = await RuleEngine(db).evaluate(
            event,
            session_id="sess-1",
            variables=self._variables(loaded=True),
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert (
            'Use `gcode grep "pattern" [PATH...] -m 50` for exact text search, '
            'or `gcode search-content "query" [PATH...]` for ranked content search.'
        ) in response.reason

    @pytest.mark.asyncio
    async def test_gcode_navigation_is_allowed_and_sets_turn_flag(self, db) -> None:
        _sync_bundled(db)
        variables = self._variables(loaded=True)
        before = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Bash",
                "command": 'gcode grep "pattern" src -m 50',
                "canonical_tool_kind": "search",
                "canonical_code_index_navigation": True,
                "canonical_code_navigation_action": "search",
            },
        )
        after = self._event(
            HookEventType.AFTER_TOOL,
            {
                "tool_name": "Bash",
                "command": 'gcode grep "pattern" src -m 50',
                "canonical_tool_kind": "search",
                "canonical_code_index_navigation": True,
                "is_error": False,
            },
        )

        allowed = await RuleEngine(db).evaluate(before, session_id="sess-1", variables=variables)
        await RuleEngine(db).evaluate(after, session_id="sess-1", variables=variables)

        assert allowed.decision == "allow"
        assert variables["code_index_navigation_used_this_turn"] is True

    @pytest.mark.asyncio
    async def test_turn_start_resets_gcode_navigation_flag(self, db) -> None:
        _sync_bundled(db)
        variables = self._variables(loaded=True, used=True)
        event = self._event(HookEventType.BEFORE_AGENT, {"prompt": "continue"})

        await RuleEngine(db).evaluate(event, session_id="sess-1", variables=variables)

        assert variables["code_index_navigation_used_this_turn"] is False

    @pytest.mark.asyncio
    async def test_broad_cat_blocks_but_tight_line_read_allows(self, db) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = self._variables(loaded=True)
        broad = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Bash",
                "command": "cat src/app.py",
                "canonical_tool_kind": "read",
                "canonical_file_path": "src/app.py",
                "canonical_code_navigation_action": "read",
                "canonical_code_navigation_broad": True,
                "canonical_source_read_scope": "full_file",
            },
        )
        narrow = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Bash",
                "command": "sed -n '1,40p' src/app.py",
                "canonical_tool_kind": "read",
                "canonical_file_path": "src/app.py",
                "canonical_code_navigation_action": "read",
                "canonical_code_navigation_broad": False,
                "canonical_narrow_source_context": True,
                "canonical_source_line_count": 40,
                "canonical_source_read_scope": "line_range",
            },
        )

        broad_response = await engine.evaluate(broad, session_id="sess-1", variables=variables)
        narrow_response = await engine.evaluate(narrow, session_id="sess-1", variables=variables)

        assert broad_response.decision == "block"
        assert broad_response.reason is not None
        assert (
            "Use `gcode outline path/to/file` to inspect file structure or "
            "`gcode symbol <id>` to retrieve a target symbol before broad source reads."
        ) in broad_response.reason
        assert narrow_response.decision == "allow"

    @pytest.mark.asyncio
    async def test_wide_line_read_requires_prior_gcode_navigation(self, db) -> None:
        _sync_bundled(db)
        event = self._event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Bash",
                "command": "sed -n '1,80p' src/app.py",
                "canonical_tool_kind": "read",
                "canonical_file_path": "src/app.py",
                "canonical_code_navigation_action": "read",
                "canonical_code_navigation_broad": True,
                "canonical_source_line_count": 80,
                "canonical_source_read_scope": "line_range",
            },
        )

        blocked = await RuleEngine(db).evaluate(
            event,
            session_id="sess-1",
            variables=self._variables(loaded=True, used=False),
        )
        allowed = await RuleEngine(db).evaluate(
            event,
            session_id="sess-1",
            variables=self._variables(loaded=True, used=True),
        )

        assert blocked.decision == "block"
        assert allowed.decision == "allow"


class TestContext7RuleCondition:
    """Test the context7 onboarding rule against canonical write metadata."""

    CONDITION = (
        "variables.get('context7_available', true) "
        "and not skill_loaded('context7') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and event.data.get('canonical_file_path', '').rpartition('.')[2] "
        "in ('py', 'rs', 'ts', 'tsx', 'js', 'jsx', 'go', 'java', 'rb', "
        "'c', 'cpp', 'h', 'hpp', 'cs', 'kt', 'swift', 'scala')"
    )

    def _eval(
        self,
        canonical_file_path: str,
        *,
        canonical_tool_kind: str = "write",
        loaded_skills: list[str] | None = None,
        injected_skills: list[str] | None = None,
        context7_available: bool = True,
    ) -> bool:
        variables: dict[str, object] = {
            "loaded_skills": loaded_skills or [],
            "context7_available": context7_available,
        }
        if injected_skills is not None:
            variables["injected_skills"] = injected_skills
        context = {
            "variables": variables,
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": canonical_file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    def test_matches_supported_write(self) -> None:
        assert self._eval("/project/src/main.ts") is True

    def test_skips_non_write(self) -> None:
        assert self._eval("/project/src/main.ts", canonical_tool_kind="read") is False

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval("/project/src/main.ts", loaded_skills=["context7"]) is False

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/main.ts", injected_skills=["context7"]) is True

    def test_skips_when_context7_unavailable(self) -> None:
        assert self._eval("/project/src/main.ts", context7_available=False) is False
