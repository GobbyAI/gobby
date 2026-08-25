"""Tests for session-defaults variable sync and rule sync mechanics."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.sync_rules import sync_bundled_rules
from gobby.workflows.sync_variables import sync_bundled_variables

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


@pytest.fixture
def rules_dir(tmp_path) -> Path:
    d = tmp_path / "rules"
    d.mkdir()
    return d


class TestSessionDefaultsSync:
    """Test that session-defaults.yaml syncs as proper rules."""

    def test_session_defaults_syncs_as_rules(self, db, rules_dir) -> None:
        """session-defaults.yaml with 'rules' key should create rule rows."""
        (rules_dir / "session-defaults.yaml").write_text(
            """
group: session-defaults
tags: [initialization]

rules:
  init-mode-level:
    description: "Default mode_level to 2"
    event: session_start
    priority: 1
    enabled: true
    effect:
      type: set_variable
      variable: mode_level
      value: 2

  init-stop-attempts:
    description: "Default stop_attempts to 0"
    event: session_start
    priority: 1
    enabled: true
    effect:
      type: set_variable
      variable: stop_attempts
      value: 0
"""
        )
        result = sync_bundled_rules(db, rules_dir)

        assert result["synced"] == 2
        assert result["errors"] == []

        # Verify rules exist in DB
        from gobby.storage.definitions.rules import RuleDefinitionManager

        mgr = RuleDefinitionManager(db)
        mode_rule = mgr.get_by_name("init-mode-level")
        assert mode_rule is not None
        assert mode_rule.enabled

        stop_rule = mgr.get_by_name("init-stop-attempts")
        assert stop_rule is not None
        assert stop_rule.enabled


class TestBundledRulesSync:
    """Test that bundled rules sync correctly."""

    def test_bundled_rules_sync_to_db(self, db) -> None:
        """Bundled rules should sync to DB without errors."""
        from gobby.workflows.sync_rules import get_bundled_rules_path

        result = sync_bundled_rules(db, get_bundled_rules_path())
        assert result["errors"] == [], f"Sync errors: {result['errors']}"


class TestBundledVariablesSync:
    """Test that bundled variable definitions sync via multi-variable format."""

    def test_bundled_variables_dir_exists(self) -> None:
        """The bundled variables directory should exist with YAML files."""
        from gobby.workflows.sync_variables import get_bundled_variables_path

        var_dir = get_bundled_variables_path()
        assert var_dir.is_dir(), f"Expected {var_dir} to be a directory"
        yaml_files = list(var_dir.glob("*.yaml"))
        assert len(yaml_files) >= 1, f"Expected >= 1 variable files, got {len(yaml_files)}"

    def test_bundled_variables_sync_to_db(self, db) -> None:
        """Bundled variable definitions should sync to DB without errors."""
        result = sync_bundled_variables(db)
        assert result["errors"] == [], f"Sync errors: {result['errors']}"
        assert result["synced"] > 0, "Expected at least one variable to sync"

    def test_synced_variables_have_correct_type(self, db) -> None:
        """All synced variables should land on the typed table as installed."""
        from gobby.storage.definitions import SessionVariableDefaultManager

        sync_bundled_variables(db)
        rows = SessionVariableDefaultManager(db).list_all(include_deleted=False)
        assert len(rows) >= 18
        for row in rows:
            assert row.source == "installed"

    def test_multi_variable_file_format(self, db, tmp_path) -> None:
        """A file with variables: dict should create multiple variable rows."""
        from gobby.storage.definitions import SessionVariableDefaultManager

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        (var_dir / "test-vars.yaml").write_text(
            """
tags: [test-tag]

variables:
  my_var_a:
    value: true
    description: Variable A
  my_var_b:
    value: 42
    description: Variable B
"""
        )

        result = sync_bundled_variables(db, variables_path=var_dir)

        assert result["synced"] == 2
        assert result["errors"] == []

        mgr = SessionVariableDefaultManager(db)
        var_a = mgr.get_by_name("my_var_a")
        assert var_a is not None
        assert var_a.default_value is True
        assert "test-tag" in (var_a.tags or [])

        var_b = mgr.get_by_name("my_var_b")
        assert var_b is not None
        assert var_b.default_value == 42

    def test_variable_idempotent_resync(self, db) -> None:
        """Running sync twice should skip already-synced variables."""
        result1 = sync_bundled_variables(db)
        assert result1["synced"] > 0, "Expected at least one variable to sync"
        first_synced = result1["synced"]

        result2 = sync_bundled_variables(db)
        assert result2["synced"] == 0
        assert result2["skipped"] == first_synced

    def test_variable_orphan_cleanup(self, db, tmp_path) -> None:
        """Variables removed from disk should be soft-deleted."""
        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        (var_dir / "vars.yaml").write_text(
            """
variables:
  temp_var:
    value: hello
"""
        )

        sync_bundled_variables(db, variables_path=var_dir)

        # Remove from disk
        (var_dir / "vars.yaml").write_text(
            """
variables:
  other_var:
    value: world
"""
        )
        result = sync_bundled_variables(db, variables_path=var_dir)

        assert result["orphaned"] == 1

    def test_all_expected_variables_synced(self, db) -> None:
        """Expected session-default variables should be synced.

        Note: task_ref was removed — claimed_tasks map handles this now.
        """
        from gobby.storage.definitions import SessionVariableDefaultManager

        sync_bundled_variables(db)
        mgr = SessionVariableDefaultManager(db)

        expected_vars = {
            "require_uv",
            "chat_mode",
            "mode_level",
            "stop_attempts",
            "max_stop_attempts",
            "max_consecutive_blocked_tool_attempts",
            "task_claimed",
            "active_task_id",
            "task_edited_files",
            "require_task_before_edit",
            "require_commit_before_status",
            "enforce_tool_schema_check",
            "auto_inject_handoff",
            "_memory_initial_stop_checked",
            "_memory_reminder_turn_seq",
            "_memory_pending_task_reviews",
            "_memory_task_review_records",
            "plan_memory_write_nudge_fired",
            "servers_listed",
            "listed_servers",
            "unlocked_tools",
        }

        rows = mgr.list_all(include_deleted=False)
        synced_names = {r.name for r in rows}
        assert expected_vars.issubset(synced_names), f"Missing: {expected_vars - synced_names}"

    def test_plan_memory_write_nudge_defaults_false(self, db: HubDatabase) -> None:
        from gobby.storage.definitions import SessionVariableDefaultManager

        sync_bundled_variables(db)
        row = SessionVariableDefaultManager(db).get_by_name("plan_memory_write_nudge_fired")
        assert row is not None
        assert row.default_value is False


_PROJECT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_PROJECT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_PROJECT_NONE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
_SESS_A = "11111111-1111-4111-8111-111111111111"
_SESS_B = "22222222-2222-4222-8222-222222222222"
_SESS_NONE = "33333333-3333-4333-8333-333333333333"


def _seed_project_scoped_defaults(db: HubDatabase) -> None:
    from gobby.storage.definitions import SessionVariableDefaultManager

    for project_id, name in (
        (_PROJECT_A, "proj-a"),
        (_PROJECT_B, "proj-b"),
        (_PROJECT_NONE, "proj-none"),
    ):
        db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (project_id, name))
    for session_id, project_id in (
        (_SESS_A, _PROJECT_A),
        (_SESS_B, _PROJECT_B),
        (_SESS_NONE, _PROJECT_NONE),
    ):
        db.execute(
            "INSERT INTO sessions (id, external_id, machine_id, source, project_id, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP)",
            (
                session_id,
                f"ext-{session_id}",
                "21000000-0000-4000-8000-000000000001",
                "claude",
                project_id,
            ),
        )
    mgr = SessionVariableDefaultManager(db)
    mgr.create(name="theme", default_value="global")
    mgr.create(name="only_global", default_value="g")
    mgr.create(name="theme", default_value="alpha", project_id=_PROJECT_A)
    mgr.create(name="only_a", default_value="a", project_id=_PROJECT_A)
    mgr.create(name="theme", default_value="beta", project_id=_PROJECT_B)


def _expected_for(project_id: str | None) -> dict[str, object]:
    if project_id == _PROJECT_A:
        return {"theme": "alpha", "only_global": "g", "only_a": "a"}
    if project_id == _PROJECT_B:
        return {"theme": "beta", "only_global": "g"}
    return {"theme": "global", "only_global": "g"}


def test_project_scoped_defaults_isolation(db: HubDatabase) -> None:
    """Alternating project A / B / none sees own overrides plus globals."""
    from gobby.mcp_proxy.tools.apply_persona import build_persona_changes
    from gobby.workflows.definitions import AgentDefinitionBody
    from gobby.workflows.state_manager import SessionVariableManager
    from gobby.workflows.variable_defaults import (
        load_variable_defaults,
        merge_unloaded_variable_defaults,
        resolve_session_project_id,
    )

    _seed_project_scoped_defaults(db)
    agent = AgentDefinitionBody(
        prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
        name="default",
    )
    sv_mgr = SessionVariableManager(db)

    assert load_variable_defaults(db, None) == _expected_for(None)
    for session_id, project_id in (
        (_SESS_A, _PROJECT_A),
        (_SESS_B, _PROJECT_B),
        (_SESS_NONE, _PROJECT_NONE),
        (_SESS_A, _PROJECT_A),
        (_SESS_B, _PROJECT_B),
        (_SESS_NONE, _PROJECT_NONE),
    ):
        expected = _expected_for(project_id)
        assert resolve_session_project_id(db, session_id) == project_id
        assert load_variable_defaults(db, project_id) == expected

        applied = sv_mgr.get_variables(session_id)
        for key, value in expected.items():
            assert applied[key] == value
        assert applied.get("only_a") == (expected.get("only_a"))

        lazy_vars: dict[str, object] = {}
        merged = merge_unloaded_variable_defaults(db, session_id, lazy_vars)
        for key, value in expected.items():
            assert merged[key] == value
        assert merged["_variable_defaults_loaded"] is True

        changes, _, _ = build_persona_changes(agent_body=agent, session_id=session_id, db=db)
        for key, value in expected.items():
            assert changes[key] == value
        assert changes.get("only_a") == expected.get("only_a")
