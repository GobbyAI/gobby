"""Tests for skill-discovery rules.

Verifies inject-python-skill, inject-rust-skill, and reset-skill-injection
rules sync correctly, have valid structure, and evaluate conditions properly.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
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
    "inject-python-skill",
    "inject-rust-skill",
    "reset-skill-injection",
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
        context = {
            "variables": {
                "loaded_skills": loaded_skills or [],
                "injected_skills": injected_skills or [],
            },
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

    def test_skips_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/main.py", injected_skills=["python"]) is False

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
        context = {
            "variables": {
                "loaded_skills": loaded_skills or [],
                "injected_skills": injected_skills or [],
            },
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

    def test_skips_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/main.rs", injected_skills=["rust"]) is False

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
        context = {
            "variables": {
                "loaded_skills": loaded_skills or [],
                "injected_skills": injected_skills or [],
            },
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

    def test_skips_when_legacy_injected(self) -> None:
        assert self._eval(canonical_tool_kind="search", injected_skills=["code-index"]) is False


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
        context = {
            "variables": {
                "loaded_skills": loaded_skills or [],
                "injected_skills": injected_skills or [],
                "context7_available": context7_available,
            },
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

    def test_skips_when_legacy_injected(self) -> None:
        assert self._eval("/project/src/main.ts", injected_skills=["context7"]) is False

    def test_skips_when_context7_unavailable(self) -> None:
        assert self._eval("/project/src/main.ts", context7_available=False) is False
