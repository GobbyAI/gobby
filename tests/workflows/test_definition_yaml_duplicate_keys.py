"""Duplicate mapping keys refuse the whole definition in every definition ingest path."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from gobby.mcp_proxy.tools.workflows._pipelines import create_pipeline_definition
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.imports import sync_imported_workflow_file
from gobby.workflows.rule_validation_yaml import validate_rule_yaml
from gobby.workflows.sync_pipelines import sync_bundled_pipelines
from gobby.workflows.sync_variables import sync_bundled_variables

pytestmark = pytest.mark.unit

_DUPLICATE_RULE_FILE = (
    "rules:\n"
    "  guarded-rule:\n"
    "    event: before_tool\n"
    "    effects:\n"
    "      - type: block\n"
    "        reason: first\n"
    "    when: 'True'\n"
    "    when: 'False'\n"
)


def test_rule_preflight_reports_duplicate_key() -> None:
    (report,) = validate_rule_yaml(_DUPLICATE_RULE_FILE)

    assert not report.valid
    assert any("duplicate mapping key" in issue.message for issue in report.errors), report


def test_project_import_refuses_duplicate_key(temp_db: HubDatabase, tmp_path: Path) -> None:
    path = tmp_path / "imported.yaml"
    path.write_text(_pipeline("imported-pipeline", "First.") + "description: Second.\n")

    with pytest.raises(yaml.YAMLError, match="duplicate mapping key"):
        sync_imported_workflow_file(temp_db, path, None)
    assert PipelineDefinitionManager(temp_db).get_by_name("imported-pipeline") is None


def test_pipeline_authoring_refuses_duplicate_key(temp_db: HubDatabase) -> None:
    manager = PipelineDefinitionManager(temp_db)

    result = create_pipeline_definition(
        manager,
        MagicMock(),
        _pipeline("authored-pipeline", "First.") + "description: Second.\n",
    )

    assert result["success"] is False
    assert "duplicate mapping key" in result["error"]
    assert manager.get_by_name("authored-pipeline") is None


def _pipeline(name: str, description: str) -> str:
    return (
        f"name: {name}\ntype: pipeline\ndescription: {description}\n"
        "steps:\n  - id: probe\n    mcp:\n      server: gobby-sessions\n      tool: list_sessions\n"
    )


def test_pipeline_duplicate_key_refused_while_siblings_sync_and_installed_row_survives(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    pip_dir = tmp_path / "pipelines"
    pip_dir.mkdir()
    (pip_dir / "kept.yaml").write_text(_pipeline("kept-pipeline", "First."))
    with patch("gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pip_dir):
        assert sync_bundled_pipelines(temp_db)["synced"] == 1

        (pip_dir / "kept.yaml").write_text(
            _pipeline("kept-pipeline", "Changed.") + "description: Second.\n"
        )
        (pip_dir / "fresh.yaml").write_text(_pipeline("fresh-pipeline", "New."))

        result = sync_bundled_pipelines(temp_db)

    assert any(
        "kept.yaml" in error and "duplicate mapping key" in error for error in result["errors"]
    ), result
    assert result["synced"] == 1, result
    assert result["orphaned"] == 0, result
    manager = PipelineDefinitionManager(temp_db)
    kept = manager.get_by_name("kept-pipeline")
    assert kept is not None
    assert kept.deleted_at is None
    assert manager.get_by_name("fresh-pipeline") is not None


def test_variable_duplicate_key_refused_while_siblings_sync_and_installed_row_survives(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    var_dir = tmp_path / "variables"
    var_dir.mkdir()
    (var_dir / "kept.yaml").write_text(
        'variables:\n  kept_var:\n    value: "first"\n    description: "First."\n'
    )
    assert sync_bundled_variables(temp_db, variables_path=var_dir)["synced"] == 1

    (var_dir / "kept.yaml").write_text(
        'variables:\n  kept_var:\n    value: "first"\n    value: "second"\n'
        '    description: "First."\n'
    )
    (var_dir / "fresh.yaml").write_text(
        'variables:\n  fresh_var:\n    value: "new"\n    description: "New."\n'
    )

    result = sync_bundled_variables(temp_db, variables_path=var_dir)

    assert any(
        "kept.yaml" in error and "duplicate mapping key" in error for error in result["errors"]
    ), result
    assert result["synced"] == 1, result
    assert result["orphaned"] == 0, result
    manager = SessionVariableDefaultManager(temp_db)
    kept = manager.get_by_name("kept_var")
    assert kept is not None
    assert kept.deleted_at is None
    assert manager.get_by_name("fresh_var") is not None
