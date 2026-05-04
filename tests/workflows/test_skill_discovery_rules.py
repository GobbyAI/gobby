"""Tests for skill-discovery rules.

Verifies inject-python-skill, inject-rust-skill, and reset-skill-injection
rules sync correctly, have valid structure, and evaluate conditions properly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
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
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_skill_discovery.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
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
    "inject-python-skill",
    "inject-rust-skill",
    "reset-skill-injection",
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
        assert 'Call get_skill(name="loading-skills") on gobby-skills, then continue.' in (
            response.context
        )
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


# --- inject-python-skill structure ---


class TestInjectPythonSkillStructure:
    """Verify inject-python-skill rule structure."""

    def test_is_before_tool_event(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-python-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"

    def test_has_block_effect_with_canonical_directive(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-python-skill")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert 'Call get_skill(name="python") on gobby-skills, then continue.' in (
            body.effects[0].reason or ""
        )


# --- inject-python-skill condition evaluation ---


class TestInjectPythonSkillCondition:
    """Test the inject-python-skill condition evaluates correctly."""

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


# --- inject-rust-skill structure ---


class TestInjectRustSkillStructure:
    """Verify inject-rust-skill rule structure."""

    def test_is_before_tool_event(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-rust-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"

    def test_has_block_effect_with_canonical_directive(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-rust-skill")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert 'Call get_skill(name="rust") on gobby-skills, then continue.' in (
            body.effects[0].reason or ""
        )


# --- inject-rust-skill condition evaluation ---


class TestInjectRustSkillCondition:
    """Test the inject-rust-skill condition evaluates correctly."""

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
        "not skill_loaded('code-index') and ("
        "(event.data.get('canonical_tool_kind') == 'read' "
        "and event.data.get('canonical_file_path', '').rpartition('.')[2] "
        "in ('py', 'rs', 'ts', 'tsx', 'js', 'jsx', 'go', 'java', 'rb', "
        "'c', 'cpp', 'h', 'hpp', 'cs', 'kt', 'swift', 'scala')) "
        "or event.data.get('canonical_tool_kind') == 'search')"
    )

    def _eval(
        self,
        *,
        canonical_tool_kind: str,
        canonical_file_path: str = "",
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
                    "canonical_file_path": canonical_file_path,
                }
            ),
            "tool_input": {},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    def test_matches_code_file_read(self) -> None:
        assert self._eval(canonical_tool_kind="read", canonical_file_path="/repo/app.py") is True

    def test_matches_search(self) -> None:
        assert self._eval(canonical_tool_kind="search") is True

    def test_skips_non_code_read(self) -> None:
        assert (
            self._eval(canonical_tool_kind="read", canonical_file_path="/repo/README.md") is False
        )

    def test_skips_when_already_loaded(self) -> None:
        assert self._eval(canonical_tool_kind="search", loaded_skills=["code-index"]) is False

    def test_does_not_skip_when_legacy_injected(self) -> None:
        assert self._eval(canonical_tool_kind="search", injected_skills=["code-index"]) is True


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
