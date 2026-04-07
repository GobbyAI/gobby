"""Tests for skill-discovery rules.

Verifies inject-python-skill, inject-rust-skill, reset-skill-injection,
and suggest-skills-on-prompt rules sync correctly, have valid structure,
and evaluate conditions properly.
"""

from __future__ import annotations

import json

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.sync import sync_bundled_rules

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
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")
    return result


SKILL_DISCOVERY_RULES = {
    "inject-python-skill",
    "inject-rust-skill",
    "reset-skill-injection",
    "suggest-skills-on-prompt",
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


# --- inject-python-skill structure ---


class TestInjectPythonSkillStructure:
    """Verify inject-python-skill rule structure."""

    def test_is_before_tool_event(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-python-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"

    def test_has_mcp_call_and_set_variable_effects(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-python-skill")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert len(body.effects) == 2
        assert body.effects[0].type == "mcp_call"
        assert body.effects[0].server == "gobby-skills"
        assert body.effects[0].tool == "get_skill"
        assert body.effects[0].inject_result is True
        assert body.effects[1].type == "set_variable"
        assert body.effects[1].variable == "injected_skills"


# --- inject-python-skill condition evaluation ---


class TestInjectPythonSkillCondition:
    """Test the inject-python-skill condition evaluates correctly."""

    CONDITION = (
        "'python' not in variables.get('injected_skills', []) "
        "and event.data.get('tool_name') == 'Read' "
        "and tool_input.get('file_path', '').endswith('.py')"
    )

    def _eval(
        self,
        file_path: str,
        *,
        tool_name: str = "Read",
        injected_skills: list[str] | None = None,
    ) -> bool:
        context = {
            "variables": {"injected_skills": injected_skills or []},
            "event": type("E", (), {"data": {"tool_name": tool_name}})(),
            "tool_input": {"file_path": file_path},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    def test_matches_python_file(self) -> None:
        assert self._eval("/project/src/main.py") is True

    def test_matches_nested_python_file(self) -> None:
        assert self._eval("/project/src/gobby/deep/module.py") is True

    def test_skips_non_python_file(self) -> None:
        assert self._eval("/project/config.yaml") is False

    def test_skips_rust_file(self) -> None:
        assert self._eval("/project/src/main.rs") is False

    def test_skips_when_already_injected(self) -> None:
        assert self._eval("/project/src/main.py", injected_skills=["python"]) is False

    def test_skips_non_read_tool(self) -> None:
        assert self._eval("/project/src/main.py", tool_name="Edit") is False

    def test_skips_bash_tool(self) -> None:
        assert self._eval("/project/src/main.py", tool_name="Bash") is False

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

    def test_has_mcp_call_and_set_variable_effects(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-rust-skill")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert len(body.effects) == 2
        assert body.effects[0].type == "mcp_call"
        assert body.effects[0].server == "gobby-skills"
        assert body.effects[0].tool == "get_skill"
        assert body.effects[0].inject_result is True
        assert body.effects[1].type == "set_variable"
        assert body.effects[1].variable == "injected_skills"


# --- inject-rust-skill condition evaluation ---


class TestInjectRustSkillCondition:
    """Test the inject-rust-skill condition evaluates correctly."""

    CONDITION = (
        "'rust' not in variables.get('injected_skills', []) "
        "and event.data.get('tool_name') == 'Read' "
        "and tool_input.get('file_path', '').endswith('.rs')"
    )

    def _eval(
        self,
        file_path: str,
        *,
        tool_name: str = "Read",
        injected_skills: list[str] | None = None,
    ) -> bool:
        context = {
            "variables": {"injected_skills": injected_skills or []},
            "event": type("E", (), {"data": {"tool_name": tool_name}})(),
            "tool_input": {"file_path": file_path},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    def test_matches_rust_file(self) -> None:
        assert self._eval("/project/src/main.rs") is True

    def test_matches_nested_rust_file(self) -> None:
        assert self._eval("/project/src/deep/lib.rs") is True

    def test_skips_non_rust_file(self) -> None:
        assert self._eval("/project/config.yaml") is False

    def test_skips_python_file(self) -> None:
        assert self._eval("/project/src/main.py") is False

    def test_skips_when_already_injected(self) -> None:
        assert self._eval("/project/src/main.rs", injected_skills=["rust"]) is False

    def test_skips_non_read_tool(self) -> None:
        assert self._eval("/project/src/main.rs", tool_name="Edit") is False

    def test_skips_empty_file_path(self) -> None:
        assert self._eval("") is False
