"""Duplicate mapping keys refuse the whole definition file in pipeline and variable sync."""

from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.sync_pipelines import sync_bundled_pipelines
from gobby.workflows.sync_variables import sync_bundled_variables

pytestmark = pytest.mark.unit


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
    with patch(
        "gobby.workflows.sync_pipelines.get_bundled_pipelines_path", return_value=pip_dir
    ):
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
