"""Integration coverage for project verification during gobby init."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli

pytestmark = [pytest.mark.integration]


def test_init_succeeds_with_array_package_json(tmp_path: Path) -> None:
    project_dir = tmp_path / "array-package"
    project_dir.mkdir()
    (project_dir / "package.json").write_text('["not", "an", "object"]', encoding="utf-8")

    project = MagicMock()
    project.id = "project-id"
    project.name = "array-package"
    project.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    project_manager = MagicMock()
    project_manager.get_by_name.return_value = None
    project_manager.create.return_value = project

    with (
        patch("gobby.cli.load_full_config_from_db", return_value=MagicMock()),
        patch("gobby.utils.project_context.get_project_context", return_value=None),
        patch("gobby.utils.git.get_github_url", return_value=None),
        patch(
            "gobby.cli.runtime.runtime_hub_database",
            return_value=MagicMock(),
        ),
        patch("gobby.storage.projects.LocalProjectManager", return_value=project_manager),
        patch("gobby.cli.init.resolve_native_bin", return_value=None),
        patch("gobby.cli.init._maybe_install_git_hooks_for_init"),
        patch("gobby.cli.init._maybe_run_linear_setup"),
    ):
        result = CliRunner().invoke(cli, ["init", "-C", str(project_dir)])

    assert result.exit_code == 0
    assert "Initialized project 'array-package'" in result.output
    project_json = project_dir / ".gobby" / "project.json"
    saved = json.loads(project_json.read_text(encoding="utf-8"))
    assert saved["id"] == "project-id"
    assert "verification" not in saved
    project_manager.create.assert_called_once()
