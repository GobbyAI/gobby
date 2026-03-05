"""Tests for tdd-enforcement rules.

Verifies the enforce-tdd-block and enforce-tdd-track-tests rules
sync correctly, have valid structure, and evaluate conditions properly.
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
    db_path = tmp_path / "test_tdd_enforcement.db"
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


TDD_ENFORCEMENT_RULES = {
    "enforce-tdd-block",
    "enforce-tdd-track-tests",
}


# --- Sync tests ---


class TestTddEnforcementSync:
    """Test that tdd-enforcement rules sync correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """Both TDD enforcement rules should sync to workflow_definitions."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        rule_names = {r.name for r in rules}

        assert TDD_ENFORCEMENT_RULES.issubset(
            rule_names
        ), f"Missing: {TDD_ENFORCEMENT_RULES - rule_names}"

    def test_all_rules_have_group(self, db, manager) -> None:
        """All rules should have group='tdd-enforcement'."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in TDD_ENFORCEMENT_RULES:
                body = json.loads(row.definition_json)
                assert body.get("group") == "tdd-enforcement", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in TDD_ENFORCEMENT_RULES:
                body = RuleDefinitionBody.model_validate_json(row.definition_json)
                assert body.event is not None


# --- enforce-tdd-block structure ---


class TestEnforceTddBlockStructure:
    """Verify enforce-tdd-block rule structure."""

    def test_is_before_tool_event(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"

    def test_has_three_effects(self, db, manager) -> None:
        """Should have set_variable + mcp_call + block effects."""
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        effects = body.resolved_effects
        assert len(effects) == 3
        assert effects[0].type == "set_variable"
        assert effects[1].type == "mcp_call"
        assert effects[2].type == "block"

    def test_set_variable_appends_to_tdd_nudged_files(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        sv_effect = body.resolved_effects[0]
        assert sv_effect.variable == "tdd_nudged_files"
        assert "tdd_nudged_files" in sv_effect.value
        assert "tool_input" in sv_effect.value

    def test_mcp_call_updates_task(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        mcp_effect = body.resolved_effects[1]
        assert mcp_effect.server == "gobby-tasks"
        assert mcp_effect.tool == "update_task"
        assert "task_id" in mcp_effect.arguments
        assert "validation_criteria" in mcp_effect.arguments

    def test_mcp_call_gated_by_task_claimed(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        mcp_effect = body.resolved_effects[1]
        assert mcp_effect.when is not None
        assert "task_claimed" in mcp_effect.when

    def test_block_targets_write_only(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        block_effect = body.resolved_effects[2]
        assert block_effect.tools == ["Write"]

    def test_when_checks_enforce_tdd(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "enforce_tdd" in body.when
        assert "tdd_nudged_files" in body.when


# --- enforce-tdd-block condition evaluation ---


class TestEnforceTddBlockCondition:
    """Test the when condition evaluates correctly for various file paths."""

    CONDITION = (
        "variables.get('enforce_tdd') "
        "and event.data.get('tool_name') == 'Write' "
        "and tool_input.get('file_path', '').endswith('.py') "
        "and not tool_input.get('file_path', '').endswith('__init__.py') "
        "and not tool_input.get('file_path', '').endswith('conftest.py') "
        "and '/tests/' not in tool_input.get('file_path', '') "
        "and not tool_input.get('file_path', '').split('/')[-1].startswith('test_') "
        "and not tool_input.get('file_path', '').endswith('_test.py') "
        "and tool_input.get('file_path', '') not in variables.get('tdd_nudged_files', [])"
    )

    def _eval(
        self,
        file_path: str,
        *,
        enforce_tdd: bool = True,
        tool_name: str = "Write",
        nudged: list[str] | None = None,
    ) -> bool:
        context = {
            "variables": {
                "enforce_tdd": enforce_tdd,
                "tdd_nudged_files": nudged or [],
            },
            "event": type("E", (), {"data": {"tool_name": tool_name}})(),
            "tool_input": {"file_path": file_path},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    def test_blocks_new_source_file(self) -> None:
        assert self._eval("/project/src/gobby/utils/helper.py") is True

    def test_blocks_nested_source_file(self) -> None:
        assert self._eval("/project/src/gobby/deep/nested/module.py") is True

    def test_skips_when_enforce_tdd_false(self) -> None:
        assert self._eval("/project/src/main.py", enforce_tdd=False) is False

    def test_skips_init_file(self) -> None:
        assert self._eval("/project/src/gobby/__init__.py") is False

    def test_skips_conftest(self) -> None:
        assert self._eval("/project/tests/conftest.py") is False

    def test_skips_test_file_by_prefix(self) -> None:
        assert self._eval("/project/test_something.py") is False

    def test_skips_test_file_in_tests_dir(self) -> None:
        assert self._eval("/project/tests/test_main.py") is False

    def test_skips_test_file_by_suffix(self) -> None:
        assert self._eval("/project/src/main_test.py") is False

    def test_skips_already_nudged_file(self) -> None:
        path = "/project/src/gobby/new_module.py"
        assert self._eval(path, nudged=[path]) is False

    def test_skips_non_python_files(self) -> None:
        assert self._eval("/project/config.yaml") is False
        assert self._eval("/project/README.md") is False
        assert self._eval("/project/data.json") is False

    def test_skips_edit_tool(self) -> None:
        assert self._eval("/project/src/main.py", tool_name="Edit") is False

    def test_skips_notebook_edit(self) -> None:
        assert self._eval("/project/src/main.py", tool_name="NotebookEdit") is False


# --- enforce-tdd-track-tests structure ---


class TestEnforceTddTrackTestsStructure:
    """Verify enforce-tdd-track-tests rule structure."""

    def test_is_after_tool_event(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-track-tests")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "after_tool"

    def test_has_set_variable_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-track-tests")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.effect.type == "set_variable"
        assert body.effect.variable == "tdd_tests_written"
        assert "tdd_tests_written" in body.effect.value

    def test_when_checks_enforce_tdd_and_tool(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-track-tests")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "enforce_tdd" in body.when
        assert "Write" in body.when
        assert "Edit" in body.when


# --- enforce-tdd-track-tests condition evaluation ---


class TestEnforceTddTrackTestsCondition:
    """Test the tracking condition evaluates correctly."""

    CONDITION = (
        "variables.get('enforce_tdd') "
        "and event.data.get('tool_name') in ('Write', 'Edit') "
        "and not event.data.get('error') "
        "and (tool_input.get('file_path', '').split('/')[-1].startswith('test_') "
        "or '/tests/' in tool_input.get('file_path', '') "
        "or tool_input.get('file_path', '').endswith('_test.py'))"
    )

    def _eval(
        self,
        file_path: str,
        *,
        enforce_tdd: bool = True,
        tool_name: str = "Write",
        error: bool = False,
    ) -> bool:
        context = {
            "variables": {"enforce_tdd": enforce_tdd},
            "event": type("E", (), {"data": {"tool_name": tool_name, "error": error}})(),
            "tool_input": {"file_path": file_path},
        }
        allowed_funcs = build_condition_helpers(context=context)
        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    def test_tracks_test_file_by_prefix(self) -> None:
        assert self._eval("/project/test_main.py") is True

    def test_tracks_test_file_in_tests_dir(self) -> None:
        assert self._eval("/project/tests/test_utils.py") is True

    def test_tracks_test_file_by_suffix(self) -> None:
        assert self._eval("/project/src/utils_test.py") is True

    def test_tracks_non_test_file_in_tests_dir(self) -> None:
        """Even non-test-prefixed files in tests/ directory are tracked."""
        assert self._eval("/project/tests/conftest.py") is True

    def test_skips_source_file(self) -> None:
        assert self._eval("/project/src/gobby/main.py") is False

    def test_skips_when_enforce_tdd_false(self) -> None:
        assert self._eval("/project/test_main.py", enforce_tdd=False) is False

    def test_skips_on_error(self) -> None:
        assert self._eval("/project/test_main.py", error=True) is False

    def test_tracks_edit_tool(self) -> None:
        assert self._eval("/project/tests/test_main.py", tool_name="Edit") is True

    def test_skips_non_write_edit_tool(self) -> None:
        assert self._eval("/project/tests/test_main.py", tool_name="Read") is False


# --- Variable definitions ---


class TestTddVariableDefinitions:
    """Verify TDD variables are defined in gobby-default-variables.yaml."""

    def test_variables_file_contains_tdd_variables(self) -> None:
        import yaml

        from gobby.workflows.sync import get_bundled_rules_path

        vars_path = get_bundled_rules_path().parent / "variables" / "gobby-default-variables.yaml"
        with open(vars_path) as f:
            data = yaml.safe_load(f)

        variables = data["variables"]
        assert "enforce_tdd" in variables
        assert variables["enforce_tdd"]["value"] is False

        assert "tdd_nudged_files" in variables
        assert variables["tdd_nudged_files"]["value"] == []

        assert "tdd_tests_written" in variables
        assert variables["tdd_tests_written"]["value"] == []
