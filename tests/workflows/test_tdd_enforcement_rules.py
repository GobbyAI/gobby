"""Tests for tdd-enforcement rules.

Verifies the enforce-tdd-block and enforce-tdd-track-tests rules
sync correctly, have valid structure, and evaluate conditions properly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _sync_bundled(db):
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return result


def _normalized_bash_event(command: str, event_type: HookEventType) -> HookEvent:
    data: dict[str, object] = {"tool_name": "Bash", "tool_input": {"command": command}}
    normalize_tool_fields(data)
    return HookEvent(
        event_type=event_type,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
    )


TDD_ENFORCEMENT_RULES = {
    "enforce-tdd-block",
    "enforce-tdd-track-tests",
}


# --- Sync tests ---


class TestTddEnforcementSync:
    """Test that tdd-enforcement rules sync correctly."""

    def test_bundled_file_syncs_all_rules(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """Both TDD enforcement rules should sync to rule_definitions."""
        _sync_bundled(db)

        rules = manager.list_all()
        rule_names = {r.name for r in rules}

        assert TDD_ENFORCEMENT_RULES.issubset(rule_names), (
            f"Missing: {TDD_ENFORCEMENT_RULES - rule_names}"
        )

    def test_all_rules_have_group(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        """All rules should have group='tdd-enforcement'."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in TDD_ENFORCEMENT_RULES:
                body = row.definition_json
                assert body.get("group") == "tdd-enforcement", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in TDD_ENFORCEMENT_RULES:
                body = RuleDefinitionBody.model_validate(row.definition_json)
                assert body.event is not None


# --- enforce-tdd-block structure ---


class TestEnforceTddBlockStructure:
    """Verify enforce-tdd-block rule structure."""

    def test_is_before_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"

    def test_has_three_effects(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        """Should have set_variable + mcp_call + block effects."""
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        effects = body.resolved_effects
        assert len(effects) == 3
        assert effects[0].type == "set_variable"
        assert effects[1].type == "mcp_call"
        assert effects[2].type == "block"

    def test_set_variable_appends_to_tdd_nudged_files(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        sv_effect = body.resolved_effects[0]
        assert sv_effect.variable == "tdd_nudged_files"
        assert "tdd_nudged_files" in sv_effect.value
        assert "first_tdd_code_path" in sv_effect.value

    def test_mcp_call_updates_task(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        mcp_effect = body.resolved_effects[1]
        assert mcp_effect.server == "gobby-tasks"
        assert mcp_effect.tool == "update_task"
        assert "task_id" in mcp_effect.arguments
        assert "validation_criteria" in mcp_effect.arguments

    def test_mcp_call_gated_by_task_claimed(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        mcp_effect = body.resolved_effects[1]
        assert mcp_effect.when is not None
        assert "task_claimed" in mcp_effect.when

    def test_block_targets_write_and_bash(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        block_effect = body.resolved_effects[2]
        assert block_effect.tools == ["Write", "Bash"]

    def test_when_checks_enforce_tdd(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-block")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.when is not None
        assert "enforce_tdd" in body.when
        assert "canonical_tool_kind" in body.when
        assert "first_tdd_code_path" in body.when
        assert "tdd_nudged_files" in body.when


# --- enforce-tdd-block condition evaluation ---


class TestEnforceTddBlockCondition:
    """Test the when condition evaluates correctly for various file paths."""

    CONDITION = (
        "variables.get('enforce_tdd') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and first_tdd_code_path(event.data, tool_input) "
        "not in ([None, ''] + variables.get('tdd_nudged_files', []))"
    )

    def _eval(
        self,
        file_path: str,
        *,
        enforce_tdd: bool = True,
        canonical_tool_kind: str = "write",
        nudged: list[str] | None = None,
    ) -> bool:
        event_data = {
            "canonical_tool_kind": canonical_tool_kind,
            "canonical_file_path": file_path,
            "canonical_file_paths": [file_path],
        }
        context = {
            "variables": {
                "enforce_tdd": enforce_tdd,
                "tdd_nudged_files": nudged or [],
            },
            "event": type("E", (), {"data": event_data})(),
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

    def test_blocks_canonical_write_from_bash(self) -> None:
        assert self._eval("/project/src/main.py", canonical_tool_kind="write") is True

    def test_skips_non_write_kind(self) -> None:
        assert self._eval("/project/src/main.py", canonical_tool_kind="read") is False


# --- enforce-tdd-track-tests structure ---


class TestEnforceTddTrackTestsStructure:
    """Verify enforce-tdd-track-tests rule structure."""

    def test_is_after_tool_event(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-track-tests")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "after_tool"

    def test_has_set_variable_effect(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-track-tests")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.effects[0].type == "set_variable"
        assert body.effects[0].variable == "tdd_tests_written"
        assert "tdd_tests_written" in body.effects[0].value
        assert "first_tdd_test_path" in body.effects[0].value

    def test_when_checks_enforce_tdd_and_tool(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("enforce-tdd-track-tests")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.when is not None
        assert "enforce_tdd" in body.when
        assert "canonical_tool_kind" in body.when
        assert "first_tdd_test_path" in body.when


# --- enforce-tdd-track-tests condition evaluation ---


class TestEnforceTddTrackTestsCondition:
    """Test the tracking condition evaluates correctly."""

    CONDITION = (
        "variables.get('enforce_tdd') "
        "and event.data.get('canonical_tool_kind') == 'write' "
        "and not event.data.get('error') "
        "and first_tdd_test_path(event.data, tool_input)"
    )

    def _eval(
        self,
        file_path: str,
        *,
        enforce_tdd: bool = True,
        canonical_tool_kind: str = "write",
        error: bool = False,
    ) -> bool:
        event_data = {
            "canonical_tool_kind": canonical_tool_kind,
            "canonical_file_path": file_path,
            "canonical_file_paths": [file_path],
            "error": error,
        }
        context = {
            "variables": {"enforce_tdd": enforce_tdd},
            "event": type("E", (), {"data": event_data})(),
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

    def test_tracks_bash_canonical_write(self) -> None:
        assert self._eval("/project/tests/test_main.py", canonical_tool_kind="write") is True

    def test_skips_non_write_kind(self) -> None:
        assert self._eval("/project/tests/test_main.py", canonical_tool_kind="read") is False


# --- Variable definitions ---


class TestTddVariableDefinitions:
    """Verify TDD variables are defined in gobby-default-variables.yaml."""

    def test_variables_file_contains_tdd_variables(self) -> None:
        import yaml

        from gobby.workflows.sync_rules import get_bundled_rules_path

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
