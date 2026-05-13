"""Tests for workflow definition synchronization split modules.

Tests sync edge cases, error handling, orphan cleanup, and variable sync.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path: Path) -> LocalDatabase:
    """Create a temporary database for sync tests."""
    db_path = tmp_path / "test_sync.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


# ═══════════════════════════════════════════════════════════════════════
# resolve_sync_placeholders
# ═══════════════════════════════════════════════════════════════════════


class TestResolveSyncPlaceholders:
    """Tests for resolve_sync_placeholders."""

    def test_no_placeholder_returns_unchanged(self) -> None:
        from gobby.workflows.sync_rules import resolve_sync_placeholders

        result = resolve_sync_placeholders('{"event": "before_tool"}')
        assert result == '{"event": "before_tool"}'

    def test_replaces_gobby_bin_with_which(self) -> None:
        from gobby.workflows.sync_rules import resolve_sync_placeholders

        with patch("gobby.workflows.sync_rules.shutil.which", return_value="/usr/local/bin/gobby"):
            result = resolve_sync_placeholders("run {{ gobby_bin }} tasks list")
            assert result == "run /usr/local/bin/gobby tasks list"

    def test_falls_back_to_python_m_gobby(self) -> None:
        from gobby.workflows.sync_rules import resolve_sync_placeholders

        with patch("gobby.workflows.sync_rules.shutil.which", return_value=None):
            result = resolve_sync_placeholders("run {{ gobby_bin }} tasks")
            assert "-m gobby" in result
            assert "{{ gobby_bin }}" not in result


# ═══════════════════════════════════════════════════════════════════════
# sync_bundled_rules
# ═══════════════════════════════════════════════════════════════════════


class TestSyncBundledRules:
    """Tests for sync_bundled_rules edge cases."""

    def test_missing_rules_path_returns_empty_result(self, db: LocalDatabase) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        result = sync_bundled_rules(db, rules_path=Path("/nonexistent/path"))
        assert result["success"] is True
        assert result["synced"] == 0

    def test_skips_non_dict_yaml(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        rule_yaml = rules_dir / "bad.yaml"
        rule_yaml.write_text("- just a list\n- not a dict\n")

        result = sync_bundled_rules(db, rules_path=rules_dir)
        assert result["synced"] == 0

    def test_skips_yaml_without_rules_key(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        rule_yaml = rules_dir / "no_rules.yaml"
        rule_yaml.write_text("name: test\ndescription: not a rule file\n")

        result = sync_bundled_rules(db, rules_path=rules_dir)
        assert result["skipped"] == 1

    def test_skips_deprecated_directory(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        rules_dir = tmp_path / "rules"
        deprecated_dir = rules_dir / "deprecated"
        deprecated_dir.mkdir(parents=True)
        rule_yaml = deprecated_dir / "old.yaml"
        rule_yaml.write_text(
            """
rules:
  old-rule:
    event: before_tool
    effect:
      type: log
      message: "old"
"""
        )

        result = sync_bundled_rules(db, rules_path=rules_dir)
        assert result["synced"] == 0

    def test_non_dict_rule_data_adds_error(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        rule_yaml = rules_dir / "bad_rule.yaml"
        rule_yaml.write_text(
            """
rules:
  bad-rule: "just a string"
"""
        )

        result = sync_bundled_rules(db, rules_path=rules_dir)
        assert len(result["errors"]) == 1
        assert "bad-rule" in result["errors"][0]

    def test_user_tag_collision_skips(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        # Create a gobby-tagged installed rule
        manager = LocalWorkflowDefinitionManager(db)
        manager.create(
            name="collision-rule",
            definition_json='{"event": "before_tool", "effects": [{"type": "log", "message": "v1"}]}',
            workflow_type="rule",
            source="installed",
            tags=["gobby"],
        )

        # User-tag sync should skip this name
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        rule_yaml = rules_dir / "collision.yaml"
        rule_yaml.write_text(
            """
rules:
  collision-rule:
    event: before_tool
    effect:
      type: log
      message: "user version"
"""
        )

        result = sync_bundled_rules(db, rules_path=rules_dir, tag="user")
        assert result["skipped"] == 1

    def test_handles_yaml_parse_error(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        rule_yaml = rules_dir / "broken.yaml"
        rule_yaml.write_text(":\n  invalid: [yaml\n  broken")

        result = sync_bundled_rules(db, rules_path=rules_dir)
        assert len(result["errors"]) >= 1

    def test_reload_cache_resync_updates_rule_event(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager, tmp_path: Path
    ) -> None:
        from gobby.mcp_proxy.tools.workflows._import import reload_cache
        from gobby.workflows.sync_rules import sync_bundled_rules

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        rule_yaml = rules_dir / "turn-end.yaml"
        rule_yaml.write_text(
            """
rules:
  bundled-rule:
    event: stop
    effect:
      type: block
      reason: "old event"
"""
        )

        with patch("gobby.workflows.sync_rules.get_bundled_rules_paths", return_value=[rules_dir]):
            sync_bundled_rules(db)

            row = manager.get_by_name("bundled-rule")
            assert row is not None
            assert '"event": "stop"' in row.definition_json

            rule_yaml.write_text(
                """
rules:
  bundled-rule:
    event: turn_end
    effect:
      type: block
      reason: "new event"
"""
            )

            loader = MagicMock()
            result = reload_cache(loader, db=db)

        loader.clear_cache.assert_called_once()
        assert result["rules_synced"] == 1

        row = manager.get_by_name("bundled-rule")
        assert row is not None
        assert '"event": "turn_end"' in row.definition_json


# ═══════════════════════════════════════════════════════════════════════
# sync_bundled_pipelines
# ═══════════════════════════════════════════════════════════════════════


class TestSyncBundledPipelines:
    """Tests for sync_bundled_pipelines edge cases."""

    def test_expand_task_fails_run_before_validation(self) -> None:
        path = Path("src/gobby/install/shared/workflows/pipelines/expand-task.yaml")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        step_ids = [step["id"] for step in data["steps"]]

        assert step_ids.index("fail_run") < step_ids.index("validate_run")
        fail_run = next(step for step in data["steps"] if step["id"] == "fail_run")
        assert fail_run["condition"] == "${{ steps.wait_run.output.status != 'completed' }}"
        assert fail_run["mcp"]["tool"] == "fail_pipeline"

    def test_missing_path_returns_error(self, db: LocalDatabase) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path",
            return_value=Path("/nonexistent"),
        ):
            result = sync_bundled_pipelines(db)
            assert len(result["errors"]) >= 1

    def test_skips_non_dict_yaml(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        pip_dir = tmp_path / "pipelines"
        pip_dir.mkdir()
        (pip_dir / "bad.yaml").write_text("- a list\n")

        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pip_dir
        ):
            result = sync_bundled_pipelines(db)
            assert result["synced"] == 0

    @pytest.mark.integration
    def test_sync_with_real_bundled_pipelines(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        result = sync_bundled_pipelines(db)

        assert result["success"] is True
        assert result["synced"] + result["skipped"] + result["updated"] >= 1
        assert result["errors"] == []

        rows = manager.list_all(workflow_type="pipeline")
        names = [row.name for row in rows]
        assert "expand-task" in names
        assert "spawn-developer" in names
        assert "spawn-qa" in names
        assert "dev" in names
        assert "qa" in names
        assert "review" in names
        assert "orchestrator" not in names
        assert "front-half-orchestrator" not in names
        assert "delivery-orchestrator" not in names

    def test_ignores_deprecated_pipeline_directory(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        pip_dir = tmp_path / "pipelines"
        deprecated_dir = pip_dir / "deprecated"
        deprecated_dir.mkdir(parents=True)
        (deprecated_dir / "old.yaml").write_text(
            """
name: old-pipeline
type: pipeline
description: Deprecated pipeline
steps: []
"""
        )

        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pip_dir
        ):
            result = sync_bundled_pipelines(db)

        assert result["success"] is True
        assert result["synced"] == 0
        assert result["updated"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == []

        rows = LocalWorkflowDefinitionManager(db).list_all(workflow_type="pipeline")
        assert [row.name for row in rows] == []

    def test_skips_yaml_without_name(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        pip_dir = tmp_path / "pipelines"
        pip_dir.mkdir()
        (pip_dir / "noname.yaml").write_text("description: no name field\ntype: pipeline\n")

        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pip_dir
        ):
            result = sync_bundled_pipelines(db)
            assert result["synced"] == 0

    def test_skips_invalid_schema(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        pip_dir = tmp_path / "pipelines"
        pip_dir.mkdir()
        # Invalid: steps must be a list, not a string
        (pip_dir / "invalid.yaml").write_text(
            "name: invalid-pipeline\ntype: pipeline\nsteps: not-a-list\n"
        )

        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pip_dir
        ):
            result = sync_bundled_pipelines(db)
            assert result["synced"] == 0

    def test_syncs_valid_pipeline(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        pip_dir = tmp_path / "pipelines"
        pip_dir.mkdir()
        (pip_dir / "good.yaml").write_text(
            """
name: test-pipeline
type: pipeline
description: A test pipeline
steps:
  - id: step1
    exec: echo hello
"""
        )

        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pip_dir
        ):
            result = sync_bundled_pipelines(db)
            assert result["synced"] == 1

    def test_syncs_root_pipeline_files(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        workflows_dir = tmp_path / "workflows"
        pip_dir = workflows_dir / "pipelines"
        pip_dir.mkdir(parents=True)
        (workflows_dir / "dev.yaml").write_text(
            """
name: dev
type: pipeline
description: Root dev pipeline
enabled: true
steps:
  - id: spawn_developer
    mcp:
      server: gobby-agents
      tool: spawn_agent
      arguments:
        agent: backend-developer
"""
        )
        (pip_dir / "spawn-developer.yaml").write_text(
            """
name: spawn-developer
type: pipeline
description: Nested dispatch pipeline
enabled: true
steps:
  - id: spawn
    mcp:
      server: gobby-agents
      tool: spawn_agent
      arguments:
        agent: backend-developer
"""
        )

        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pip_dir
        ):
            result = sync_bundled_pipelines(db)

        assert result["synced"] == 2
        rows = LocalWorkflowDefinitionManager(db).list_all(workflow_type="pipeline")
        assert {row.name for row in rows} == {"dev", "spawn-developer"}

    def test_updates_legacy_template_pipeline_row(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        workflows_dir = tmp_path / "workflows"
        pip_dir = workflows_dir / "pipelines"
        pip_dir.mkdir(parents=True)
        (pip_dir / "spawn-developer.yaml").write_text(
            """
name: spawn-developer
type: pipeline
description: Dispatch pipeline
enabled: true
inputs:
  agent:
    type: string
    default: backend-developer
steps:
  - id: spawn
    mcp:
      server: gobby-agents
      tool: spawn_agent
      arguments:
        agent: "${{ inputs.agent }}"
"""
        )
        manager.create(
            name="spawn-developer",
            definition_json=json.dumps(
                {
                    "name": "spawn-developer",
                    "type": "pipeline",
                    "description": "Old dispatch pipeline",
                    "enabled": True,
                    "inputs": {"agent": {"type": "string", "default": "developer"}},
                    "steps": [
                        {
                            "id": "spawn",
                            "mcp": {
                                "server": "gobby-agents",
                                "tool": "spawn_agent",
                                "arguments": {"agent": "${{ inputs.agent }}"},
                            },
                        }
                    ],
                }
            ),
            workflow_type="pipeline",
            source="template",
            tags=["gobby"],
        )

        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pip_dir
        ):
            result = sync_bundled_pipelines(db)

        assert result["updated"] == 1
        row = manager.get_by_name("spawn-developer")
        assert row is not None
        assert row.source == "installed"
        data = json.loads(row.definition_json)
        assert data["inputs"]["agent"]["default"] == "backend-developer"


# ═══════════════════════════════════════════════════════════════════════
# sync_bundled_variables
# ═══════════════════════════════════════════════════════════════════════


class TestSyncBundledVariables:
    """Tests for sync_bundled_variables."""

    def test_missing_path_returns_empty_result(self, db: LocalDatabase) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        result = sync_bundled_variables(db, variables_path=Path("/nonexistent"))
        assert result["success"] is True
        assert result["synced"] == 0

    def test_syncs_new_variable(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        (var_dir / "test.yaml").write_text(
            """
variables:
  my_var:
    value: "hello"
    description: "A test variable"
"""
        )

        result = sync_bundled_variables(db, variables_path=var_dir)
        assert result["synced"] == 1

    def test_skips_non_dict_yaml(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        (var_dir / "bad.yaml").write_text("- list item\n")

        result = sync_bundled_variables(db, variables_path=var_dir)
        assert result["synced"] == 0

    def test_skips_yaml_without_variables_key(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        (var_dir / "no_vars.yaml").write_text("name: not variables\n")

        result = sync_bundled_variables(db, variables_path=var_dir)
        assert result["skipped"] == 1

    def test_non_dict_variable_adds_error(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        (var_dir / "bad_var.yaml").write_text(
            """
variables:
  bad_var: "just a string"
"""
        )

        result = sync_bundled_variables(db, variables_path=var_dir)
        assert len(result["errors"]) == 1

    def test_does_not_overwrite_existing_variable(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        var_file = var_dir / "test.yaml"
        var_file.write_text(
            """
variables:
  update_var:
    value: "v1"
"""
        )

        sync_bundled_variables(db, variables_path=var_dir)

        var_file.write_text(
            """
variables:
  update_var:
    value: "v2"
"""
        )
        # Sync no longer overwrites — drift detected at runtime
        result = sync_bundled_variables(db, variables_path=var_dir)
        assert result["skipped"] == 1

    def test_skips_unchanged_variable(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        (var_dir / "same.yaml").write_text(
            """
variables:
  same_var:
    value: "constant"
"""
        )

        sync_bundled_variables(db, variables_path=var_dir)
        result = sync_bundled_variables(db, variables_path=var_dir)
        assert result["skipped"] == 1

    def test_orphan_cleanup(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        var_file = var_dir / "orphan.yaml"
        var_file.write_text(
            """
variables:
  orphan_var:
    value: "soon gone"
"""
        )

        sync_bundled_variables(db, variables_path=var_dir)
        var_file.unlink()

        result = sync_bundled_variables(db, variables_path=var_dir)
        assert result["orphaned"] >= 1

    def test_respects_soft_deleted_variable(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        (var_dir / "restore.yaml").write_text(
            """
variables:
  restore_var:
    value: "restored"
"""
        )

        sync_bundled_variables(db, variables_path=var_dir)

        manager = LocalWorkflowDefinitionManager(db)
        row = manager.get_by_name("restore_var")
        assert row is not None
        manager.delete(row.id)

        # Sync respects soft-deletes — does not re-create
        result = sync_bundled_variables(db, variables_path=var_dir)
        assert result["skipped"] == 1

    def test_handles_yaml_parse_error(self, db: LocalDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        (var_dir / "broken.yaml").write_text(":\n  invalid: [yaml\n  broken")

        result = sync_bundled_variables(db, variables_path=var_dir)
        assert len(result["errors"]) >= 1


# ═══════════════════════════════════════════════════════════════════════
# get_bundled_*_path helpers
# ═══════════════════════════════════════════════════════════════════════


class TestBundledPaths:
    """Tests for path helper functions."""

    def test_get_bundled_rules_path_returns_path(self) -> None:
        from gobby.workflows.sync_rules import get_bundled_rules_path

        result = get_bundled_rules_path()
        assert isinstance(result, Path)
        assert str(result).endswith("rules")

    def test_get_bundled_pipelines_path_returns_path(self) -> None:
        from gobby.workflows.sync_pipelines import get_bundled_pipelines_path

        result = get_bundled_pipelines_path()
        assert isinstance(result, Path)
        assert str(result).endswith("pipelines")

    def test_get_bundled_variables_path_returns_path(self) -> None:
        from gobby.workflows.sync_variables import get_bundled_variables_path

        result = get_bundled_variables_path()
        assert isinstance(result, Path)
        assert str(result).endswith("variables")
