"""Tests for workflow definition synchronization split modules.

Tests sync edge cases, error handling, orphan cleanup, and variable sync.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer

pytestmark = pytest.mark.integration


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    """Create a temporary database for sync tests."""
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


@pytest.fixture
def pipeline_manager(db: HubDatabase) -> PipelineDefinitionManager:
    return PipelineDefinitionManager(db)


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

    def test_missing_rules_path_returns_empty_result(self, db: HubDatabase) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        result = sync_bundled_rules(db, rules_path=Path("/nonexistent/path"))
        assert result["success"] is True
        assert result["synced"] == 0

    def test_skips_non_dict_yaml(self, db: HubDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        rule_yaml = rules_dir / "bad.yaml"
        rule_yaml.write_text("- just a list\n- not a dict\n")

        result = sync_bundled_rules(db, rules_path=rules_dir)
        assert result["synced"] == 0

    def test_skips_yaml_without_rules_key(self, db: HubDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        rule_yaml = rules_dir / "no_rules.yaml"
        rule_yaml.write_text("name: test\ndescription: not a rule file\n")

        result = sync_bundled_rules(db, rules_path=rules_dir)
        assert result["skipped"] == 1

    def test_sync_rule_file_imports_yml_file(
        self, db: HubDatabase, manager: RuleDefinitionManager, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_rules import sync_rule_file

        rule_file = tmp_path / "single-rule.yml"
        rule_file.write_text(
            """
rules:
  single-yml-rule:
    event: turn_start
    effect:
      type: block
      reason: "from yml"
"""
        )

        result = sync_rule_file(db, rule_file)

        assert result["synced"] == 1
        row = manager.get_by_name("single-yml-rule")
        assert row is not None
        assert row.enabled is True
        assert row.tags == ["user"]
        assert row.definition_json["event"] == "turn_start"

    def test_imported_rule_survives_bundled_orphan_cleanup(
        self, db: HubDatabase, manager: RuleDefinitionManager, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules, sync_rule_file

        imported_file = tmp_path / "imported.yaml"
        imported_file.write_text(
            "rules:\n  imported-rule:\n    event: turn_start\n    effect:\n"
            "      type: inject_context\n      template: imported\n"
        )
        sync_rule_file(db, imported_file)

        bundled_dir = tmp_path / "bundled"
        bundled_dir.mkdir()
        bundled_file = bundled_dir / "bundled.yaml"
        bundled_file.write_text(
            "rules:\n  old-bundled-rule:\n    event: turn_start\n    effect:\n"
            "      type: inject_context\n      template: old\n"
        )
        sync_bundled_rules(db, rules_path=bundled_dir)

        bundled_file.write_text(
            "rules:\n  new-bundled-rule:\n    event: turn_start\n    effect:\n"
            "      type: inject_context\n      template: new\n"
        )
        result = sync_bundled_rules(db, rules_path=bundled_dir)

        assert result["orphaned"] == 1
        assert manager.get_by_name("old-bundled-rule") is None
        imported = manager.get_by_name("imported-rule")
        assert imported is not None
        assert imported.tags == ["user"]
        bundled = manager.get_by_name("new-bundled-rule")
        assert bundled is not None
        assert bundled.tags == ["gobby"]

    def test_sync_rule_file_does_not_update_sibling_rule(
        self, db: HubDatabase, manager: RuleDefinitionManager, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_rules import sync_rule_file

        manager.create(
            name="sibling-rule",
            definition_json=json.dumps(
                {
                    "event": "turn_start",
                    "effects": [{"type": "block", "reason": "old sibling"}],
                }
            ),
            source="installed",
            tags=["gobby"],
        )

        target_file = tmp_path / "target.yml"
        target_file.write_text(
            """
rules:
  target-rule:
    event: turn_start
    effect:
      type: block
      reason: "target"
"""
        )
        sibling_file = tmp_path / "sibling.yaml"
        sibling_file.write_text(
            """
rules:
  sibling-rule:
    event: turn_start
    effect:
      type: block
      reason: "new sibling"
"""
        )

        result = sync_rule_file(db, target_file)

        assert result["synced"] == 1
        target_row = manager.get_by_name("target-rule")
        assert target_row is not None
        sibling_row = manager.get_by_name("sibling-rule")
        assert sibling_row is not None
        sibling_definition = sibling_row.definition_json
        assert sibling_definition["effects"][0]["reason"] == "old sibling"

    def test_skips_deprecated_directory(self, db: HubDatabase, tmp_path: Path) -> None:
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

    def test_non_dict_rule_data_adds_error(self, db: HubDatabase, tmp_path: Path) -> None:
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

    def test_user_tag_collision_skips(self, db: HubDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        # Create a gobby-tagged installed rule
        manager = RuleDefinitionManager(db)
        manager.create(
            name="collision-rule",
            definition_json='{"event": "before_tool", "effects": [{"type": "log", "message": "v1"}]}',
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

    def test_handles_yaml_parse_error(self, db: HubDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        rule_yaml = rules_dir / "broken.yaml"
        rule_yaml.write_text(":\n  invalid: [yaml\n  broken")

        result = sync_bundled_rules(db, rules_path=rules_dir)
        assert len(result["errors"]) >= 1
        assert result["success"] is False

    def test_parse_error_does_not_orphan_existing_rule(
        self, db: HubDatabase, manager: RuleDefinitionManager, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        rule_file = rules_dir / "rule.yaml"
        rule_file.write_text(
            "rules:\n  retained-rule:\n    event: before_tool\n    effect:\n"
            "      type: inject_context\n      template: retained\n"
        )
        sync_bundled_rules(db, rules_path=rules_dir)

        rule_file.unlink()
        (rules_dir / "broken.yaml").write_text(":\n  invalid: [yaml\n  broken")
        result = sync_bundled_rules(db, rules_path=rules_dir)

        assert result["success"] is False
        assert result["orphaned"] == 0
        assert manager.get_by_name("retained-rule") is not None

    def test_empty_directory_does_not_orphan_existing_rule(
        self, db: HubDatabase, manager: RuleDefinitionManager, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        rule_file = rules_dir / "rule.yaml"
        rule_file.write_text(
            "rules:\n  retained-rule:\n    event: before_tool\n    effect:\n"
            "      type: inject_context\n      template: retained\n"
        )
        sync_bundled_rules(db, rules_path=rules_dir)
        rule_file.unlink()

        result = sync_bundled_rules(db, rules_path=rules_dir)

        assert result["orphaned"] == 0
        assert manager.get_by_name("retained-rule") is not None

    def test_restores_soft_deleted_rule(
        self, db: HubDatabase, manager: RuleDefinitionManager, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "restore.yaml").write_text(
            "rules:\n  restore-rule:\n    event: before_tool\n    effect:\n"
            "      type: inject_context\n      template: restored\n"
        )
        sync_bundled_rules(db, rules_path=rules_dir)
        row = manager.get_by_name("restore-rule")
        assert row is not None
        manager.delete(row.id)

        result = sync_bundled_rules(db, rules_path=rules_dir)

        assert result["updated"] == 1
        assert manager.get_by_name("restore-rule") is not None

    def test_reload_cache_resync_updates_rule_event(self, db: HubDatabase, tmp_path: Path) -> None:
        from gobby.mcp_proxy.tools.workflows._import import reload_cache
        from gobby.storage.definitions.rules import RuleDefinitionManager
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
        manager = RuleDefinitionManager(db)

        with (
            patch("gobby.workflows.sync_rules.get_bundled_rules_paths", return_value=[rules_dir]),
            patch(
                "gobby.mcp_proxy.tools.workflows._import.sync_imported_workflows",
                return_value={"synced": 0, "errors": []},
            ),
        ):
            sync_bundled_rules(db)

            row = manager.get_by_name("bundled-rule")
            assert row is not None
            body = row.definition_json
            if isinstance(body, str):
                body = json.loads(body)
            assert body["event"] == "stop"

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
        body = row.definition_json
        if isinstance(body, str):
            body = json.loads(body)
        assert body["event"] == "turn_end"


# ═══════════════════════════════════════════════════════════════════════
# sync_bundled_pipelines
# ═══════════════════════════════════════════════════════════════════════


class TestSyncBundledPipelines:
    """Tests for sync_bundled_pipelines edge cases."""

    @pytest.mark.integration
    def test_expand_task_fails_run_before_validation(self) -> None:
        """Evaluate expand-task conditions with the runtime step renderer."""
        from gobby.workflows.sync_pipelines import get_bundled_pipelines_path

        path = get_bundled_pipelines_path() / "expand-task.yaml"
        assert path.is_file(), f"Missing bundled pipeline: {path}"
        pipeline = PipelineDefinition(**yaml.safe_load(path.read_text(encoding="utf-8")))
        renderer = StepRenderer(strict_conditions=True)
        fail_run = pipeline.get_step("fail_run")
        validate_run = pipeline.get_step("validate_run")
        assert fail_run is not None
        assert validate_run is not None
        assert fail_run.mcp is not None
        assert validate_run.mcp is not None
        assert fail_run.mcp.tool == "fail_pipeline"
        assert validate_run.mcp.tool == "validate_expansion_run"

        for wait_status in ("failed", "timeout", "cancelled"):
            context = {"steps": {"wait_run": {"output": {"status": wait_status}}}}
            assert renderer.should_run_step(fail_run, context) is True
            assert renderer.should_run_step(validate_run, context) is False

        completed_context = {"steps": {"wait_run": {"output": {"status": "completed"}}}}
        assert renderer.should_run_step(fail_run, completed_context) is False
        assert renderer.should_run_step(validate_run, completed_context) is True

    def test_missing_path_returns_error(self, db: HubDatabase) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path",
            return_value=Path("/nonexistent"),
        ):
            result = sync_bundled_pipelines(db)
            assert len(result["errors"]) >= 1

    def test_skips_non_dict_yaml(self, db: HubDatabase, tmp_path: Path) -> None:
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
        self, db: HubDatabase, pipeline_manager: PipelineDefinitionManager
    ) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        result = sync_bundled_pipelines(db)

        assert result["success"] is True
        assert result["synced"] + result["skipped"] + result["updated"] >= 1
        assert result["errors"] == []

        rows = pipeline_manager.list_all()
        names = [row.name for row in rows]
        assert set(names) == {"expand-task", "gobby-merge", "review"}

        expand_task = pipeline_manager.get_by_name("expand-task")
        gobby_merge = pipeline_manager.get_by_name("gobby-merge")
        review = pipeline_manager.get_by_name("review")
        assert expand_task is not None
        assert expand_task.enabled is True
        assert gobby_merge is not None
        assert gobby_merge.enabled is True
        assert review is not None
        assert review.enabled is True

    def test_ignores_deprecated_pipeline_directory(
        self, db: HubDatabase, tmp_path: Path, pipeline_manager: PipelineDefinitionManager
    ) -> None:
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

        rows = pipeline_manager.list_all()
        assert [row.name for row in rows] == []

    def test_skips_yaml_without_name(self, db: HubDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        pip_dir = tmp_path / "pipelines"
        pip_dir.mkdir()
        (pip_dir / "noname.yaml").write_text("description: no name field\ntype: pipeline\n")

        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pip_dir
        ):
            result = sync_bundled_pipelines(db)
            assert result["synced"] == 0

    def test_skips_invalid_schema(self, db: HubDatabase, tmp_path: Path) -> None:
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

    def test_rejects_non_pipeline_root_yaml(
        self, db: HubDatabase, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        workflows_dir = tmp_path / "workflows"
        pip_dir = workflows_dir / "pipelines"
        pip_dir.mkdir(parents=True)
        (workflows_dir / "rules.yaml").write_text("name: bundled-rules\ntype: step\nsteps: []\n")

        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pip_dir
        ):
            result = sync_bundled_pipelines(db)

        assert result["synced"] == 0
        assert PipelineDefinitionManager(db).get_by_name("bundled-rules") is None
        assert "Skipping non-pipeline YAML file" in caplog.text

    def test_syncs_valid_pipeline(self, db: HubDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        pip_dir = tmp_path / "pipelines"
        pip_dir.mkdir()
        (pip_dir / "good.yaml").write_text(
            """
name: test-pipeline
type: pipeline
enabled: "false"
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
            row = PipelineDefinitionManager(db).get_by_name("test-pipeline")
            assert row is not None
            assert row.enabled is False

    def test_omitted_enabled_defaults_true_and_refresh_preserves_toggle(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        pip_dir = tmp_path / "pipelines"
        pip_dir.mkdir()
        pipeline_path = pip_dir / "default-enabled.yaml"
        pipeline_path.write_text(
            """
name: default-enabled
type: pipeline
steps:
  - id: step1
    exec: echo hello
"""
        )

        manager = PipelineDefinitionManager(db)
        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pip_dir
        ):
            result = sync_bundled_pipelines(db)
            assert result["synced"] == 1
            row = manager.get_by_name("default-enabled")
            assert row is not None
            assert row.enabled is True

            manager.update(row.id, enabled=False)
            pipeline_path.write_text(
                """
name: default-enabled
type: pipeline
description: Updated definition
steps:
  - id: step1
    exec: echo hello
"""
            )
            result = sync_bundled_pipelines(db)

        assert result["updated"] == 1
        refreshed = manager.get_by_name("default-enabled")
        assert refreshed is not None
        assert refreshed.enabled is False

    def test_enabled_default_flip_updates_unmodified_pipeline(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        pip_dir = tmp_path / "pipelines"
        pip_dir.mkdir()
        pipeline_path = pip_dir / "flip.yaml"
        pipeline_path.write_text(
            """
name: flip-pipe
type: pipeline
enabled: false
steps:
  - id: step1
    exec: echo hello
"""
        )
        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pip_dir
        ):
            sync_bundled_pipelines(db)
            seeded = PipelineDefinitionManager(db).get_by_name("flip-pipe")
            assert seeded is not None
            assert seeded.enabled is False
            assert seeded.enabled_pinned is False
            pipeline_path.write_text(
                """
name: flip-pipe
type: pipeline
enabled: true
steps:
  - id: step1
    exec: echo hello
"""
            )
            result = sync_bundled_pipelines(db)
        updated = PipelineDefinitionManager(db).get_by_name("flip-pipe")
        assert result["updated"] == 1
        assert updated is not None
        assert updated.enabled is True
        assert updated.enabled_pinned is False

    def test_enabled_default_flip_preserves_pinned_pipeline(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        pip_dir = tmp_path / "pipelines"
        pip_dir.mkdir()
        pipeline_path = pip_dir / "pin.yaml"
        pipeline_path.write_text(
            """
name: pin-pipe
type: pipeline
enabled: true
steps:
  - id: step1
    exec: echo hello
"""
        )
        manager = PipelineDefinitionManager(db)
        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pip_dir
        ):
            sync_bundled_pipelines(db)
            seeded = manager.get_by_name("pin-pipe")
            assert seeded is not None
            manager.update(seeded.id, enabled=False)
            pipeline_path.write_text(
                """
name: pin-pipe
type: pipeline
enabled: true
description: changed
steps:
  - id: step1
    exec: echo hello
"""
            )
            sync_bundled_pipelines(db)
        preserved = manager.get_by_name("pin-pipe")
        assert preserved is not None
        assert preserved.enabled is False
        assert preserved.enabled_pinned is True
        assert preserved.description == "changed"

    def test_syncs_root_pipeline_files(
        self, db: HubDatabase, tmp_path: Path, pipeline_manager: PipelineDefinitionManager
    ) -> None:
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
        rows = pipeline_manager.list_all()
        assert {row.name for row in rows} == {"dev", "spawn-developer"}

    def test_updates_legacy_template_pipeline_row(
        self, db: HubDatabase, pipeline_manager: PipelineDefinitionManager, tmp_path: Path
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
        pipeline_manager.create(
            name="spawn-developer",
            definition_json={
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
            },
            source="installed",
            tags=["gobby"],
        )

        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pip_dir
        ):
            result = sync_bundled_pipelines(db)

        assert result["errors"] == []
        assert result["updated"] == 1
        row = pipeline_manager.get_by_name("spawn-developer")
        assert row is not None
        assert row.source == "installed"
        data = row.definition_json
        assert data["inputs"]["agent"]["default"] == "backend-developer"

    def test_orphan_cleanup_keeps_custom_gobby_pipeline(
        self, db: HubDatabase, pipeline_manager: PipelineDefinitionManager, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        pipelines_dir = tmp_path / "pipelines"
        pipelines_dir.mkdir()
        (pipelines_dir / "current.yaml").write_text(
            "name: current-pipeline\ntype: pipeline\n"
            "steps:\n  - id: current\n    exec: echo current\n"
        )
        installed = pipeline_manager.create(
            name="installed-pipeline",
            definition_json={
                "name": "installed-pipeline",
                "type": "pipeline",
                "steps": [{"id": "s", "exec": "echo"}],
            },
            source="installed",
            tags=["gobby"],
        )
        duplicate = pipeline_manager.duplicate(installed.id, "duplicated-pipeline")
        custom = pipeline_manager.create(
            name="custom-gobby-pipeline",
            definition_json={
                "name": "custom-gobby-pipeline",
                "type": "pipeline",
                "steps": [{"id": "s", "exec": "echo"}],
            },
            source="custom",
            tags=["gobby"],
        )

        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path",
            return_value=pipelines_dir,
        ):
            result = sync_bundled_pipelines(db)

        assert result["orphaned"] == 1
        assert pipeline_manager.get(custom.id).deleted_at is None
        duplicated_row = pipeline_manager.get(duplicate.id)
        assert duplicated_row.deleted_at is None
        assert duplicated_row.source == "custom"

    def test_orphan_cleanup_skips_empty_directory(
        self, db: HubDatabase, pipeline_manager: PipelineDefinitionManager, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        pipelines_dir = tmp_path / "pipelines"
        pipelines_dir.mkdir()
        installed = pipeline_manager.create(
            name="installed-pipeline",
            definition_json={
                "name": "installed-pipeline",
                "type": "pipeline",
                "steps": [{"id": "s", "exec": "echo"}],
            },
            source="installed",
            tags=["gobby"],
        )

        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path",
            return_value=pipelines_dir,
        ):
            result = sync_bundled_pipelines(db)

        assert result["orphaned"] == 0
        assert pipeline_manager.get(installed.id).deleted_at is None

    @pytest.mark.integration
    def test_orphan_cleanup_skips_parse_failure(
        self, db: HubDatabase, pipeline_manager: PipelineDefinitionManager, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_pipelines import sync_bundled_pipelines

        pipelines_dir = tmp_path / "pipelines"
        pipelines_dir.mkdir()
        (pipelines_dir / "broken.yaml").write_text("name: [unterminated\n")
        installed = pipeline_manager.create(
            name="installed-pipeline",
            definition_json={
                "name": "installed-pipeline",
                "type": "pipeline",
                "steps": [{"id": "s", "exec": "echo"}],
            },
            source="installed",
            tags=["gobby"],
        )

        with patch(
            "gobby.workflows.sync_pipelines.get_bundled_pipelines_path",
            return_value=pipelines_dir,
        ):
            result = sync_bundled_pipelines(db)

        assert result["errors"]
        assert result["orphaned"] == 0
        assert pipeline_manager.get(installed.id).deleted_at is None


# ═══════════════════════════════════════════════════════════════════════
# sync_bundled_variables
# ═══════════════════════════════════════════════════════════════════════


class TestSyncBundledVariables:
    """Tests for sync_bundled_variables."""

    def test_missing_path_returns_empty_result(self, db: HubDatabase) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        result = sync_bundled_variables(db, variables_path=Path("/nonexistent"))
        assert result["success"] is True
        assert result["synced"] == 0

    def test_syncs_new_variable(self, db: HubDatabase, tmp_path: Path) -> None:
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

    def test_skips_non_dict_yaml(self, db: HubDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        (var_dir / "bad.yaml").write_text("- list item\n")

        result = sync_bundled_variables(db, variables_path=var_dir)
        assert result["synced"] == 0

    def test_skips_yaml_without_variables_key(self, db: HubDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        (var_dir / "no_vars.yaml").write_text("name: not variables\n")

        result = sync_bundled_variables(db, variables_path=var_dir)
        assert result["skipped"] == 1

    def test_non_dict_variable_adds_error(self, db: HubDatabase, tmp_path: Path) -> None:
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

    def test_enabled_default_flip_updates_unmodified_variable(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        from gobby.storage.definitions import SessionVariableDefaultManager
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        var_file = var_dir / "test.yaml"
        var_file.write_text(
            """
variables:
  update_var:
    value: "v1"
    enabled: false
"""
        )
        sync_bundled_variables(db, variables_path=var_dir)
        seeded = SessionVariableDefaultManager(db).get_by_name("update_var")
        assert seeded is not None
        assert seeded.enabled is False
        assert seeded.enabled_pinned is False

        var_file.write_text(
            """
variables:
  update_var:
    value: "v2"
    enabled: true
"""
        )
        result = sync_bundled_variables(db, variables_path=var_dir)
        updated = SessionVariableDefaultManager(db).get_by_name("update_var")
        assert result["updated"] == 1
        assert updated is not None
        assert updated.default_value == "v2"
        assert updated.enabled is True
        assert updated.enabled_pinned is False

    def test_enabled_default_flip_preserves_pinned_variable(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        from gobby.storage.definitions import SessionVariableDefaultManager
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        var_file = var_dir / "test.yaml"
        var_file.write_text(
            """
variables:
  update_var:
    value: "v1"
    enabled: true
"""
        )
        sync_bundled_variables(db, variables_path=var_dir)
        mgr = SessionVariableDefaultManager(db)
        seeded = mgr.get_by_name("update_var")
        assert seeded is not None
        mgr.update(seeded.id, enabled=False)
        pinned = mgr.get_by_name("update_var")
        assert pinned is not None
        assert pinned.enabled_pinned is True

        var_file.write_text(
            """
variables:
  update_var:
    value: "v2"
    enabled: true
"""
        )
        sync_bundled_variables(db, variables_path=var_dir)
        preserved = mgr.get_by_name("update_var")
        assert preserved is not None
        assert preserved.default_value == "v2"
        assert preserved.enabled is False
        assert preserved.enabled_pinned is True

    def test_skips_unchanged_variable(self, db: HubDatabase, tmp_path: Path) -> None:
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

    def test_orphan_cleanup(self, db: HubDatabase, tmp_path: Path) -> None:
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
        (var_dir / "retained.yaml").write_text("variables:\n  retained_var:\n    value: retained\n")

        result = sync_bundled_variables(db, variables_path=var_dir)
        assert result["orphaned"] >= 1

    def test_removed_human_wait_variable_is_soft_deleted(self, db: HubDatabase) -> None:
        from gobby.storage.definitions import SessionVariableDefaultManager
        from gobby.workflows.sync_variables import sync_bundled_variables

        manager = SessionVariableDefaultManager(db)
        obsolete = manager.create(
            name="waiting_on_user_input",
            default_value=False,
            source="installed",
            tags=["gobby"],
        )

        result = sync_bundled_variables(db)

        assert result["orphaned"] == 1
        assert manager.get_by_name("waiting_on_user_input") is None
        deleted = manager.get(obsolete.id, include_deleted=True)
        assert deleted.deleted_at is not None

    def test_project_scoped_installed_variable_is_not_orphan_pruned(self, db: HubDatabase) -> None:
        from gobby.storage.definitions import SessionVariableDefaultManager
        from gobby.workflows.sync_variables import sync_bundled_variables

        project_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        db.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (project_id, "orphan-scope"),
        )
        manager = SessionVariableDefaultManager(db)
        scoped = manager.create(
            name="project_only_orphan_guard",
            default_value=True,
            source="installed",
            tags=["gobby"],
            project_id=project_id,
        )

        result = sync_bundled_variables(db)

        kept = manager.get_by_name("project_only_orphan_guard", project_id=project_id)
        assert kept is not None
        assert kept.id == scoped.id
        assert kept.deleted_at is None
        assert result["success"] is True

    def test_empty_directory_does_not_orphan_existing_variable(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        var_file = var_dir / "variable.yaml"
        var_file.write_text("variables:\n  retained_var:\n    value: retained\n")
        sync_bundled_variables(db, variables_path=var_dir)
        var_file.unlink()

        result = sync_bundled_variables(db, variables_path=var_dir)

        from gobby.storage.definitions import SessionVariableDefaultManager

        manager = SessionVariableDefaultManager(db)
        assert result["orphaned"] == 0
        assert manager.get_by_name("retained_var") is not None

    def test_restores_soft_deleted_variable(self, db: HubDatabase, tmp_path: Path) -> None:
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

        from gobby.storage.definitions import SessionVariableDefaultManager

        manager = SessionVariableDefaultManager(db)
        row = manager.get_by_name("restore_var")
        assert row is not None
        manager.delete(row.id)

        result = sync_bundled_variables(db, variables_path=var_dir)
        assert result["updated"] == 1
        assert manager.get_by_name("restore_var") is not None

    def test_handles_yaml_parse_error(self, db: HubDatabase, tmp_path: Path) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        (var_dir / "broken.yaml").write_text(":\n  invalid: [yaml\n  broken")

        result = sync_bundled_variables(db, variables_path=var_dir)
        assert len(result["errors"]) >= 1
        assert result["success"] is False

    def test_parse_error_does_not_orphan_existing_variable(
        self, db: HubDatabase, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_variables import sync_bundled_variables

        var_dir = tmp_path / "variables"
        var_dir.mkdir()
        var_file = var_dir / "variable.yaml"
        var_file.write_text("variables:\n  retained_var:\n    value: present\n")
        sync_bundled_variables(db, variables_path=var_dir)

        var_file.unlink()
        (var_dir / "broken.yaml").write_text(":\n  invalid: [yaml\n  broken")
        result = sync_bundled_variables(db, variables_path=var_dir)

        from gobby.storage.definitions import SessionVariableDefaultManager

        manager = SessionVariableDefaultManager(db)
        assert result["success"] is False
        assert result["orphaned"] == 0
        assert manager.get_by_name("retained_var") is not None


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
