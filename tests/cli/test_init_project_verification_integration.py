"""Integration coverage for project verification during gobby init."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.projects import LocalProjectManager
from tests.fixtures.isolated_checkout import insert_isolated_machine, patch_local_machine_id

pytestmark = [pytest.mark.integration]


def test_init_succeeds_with_array_package_json(
    tmp_path: Path, hub_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = tmp_path / "array-package"
    project_dir.mkdir()
    (project_dir / "package.json").write_text('["not", "an", "object"]', encoding="utf-8")

    machine_id = insert_isolated_machine(hub_db)
    patch_local_machine_id(monkeypatch, machine_id)

    with (
        patch("gobby.cli.runtime.CliRuntime.require_config", return_value=MagicMock()),
        patch("gobby.utils.project_context.get_project_context", return_value=None),
        patch("gobby.utils.git.get_github_url", return_value=None),
        patch(
            "gobby.cli.runtime.require_cli_database",
            return_value=hub_db,
        ),
        patch("gobby.cli.init.resolve_native_bin", return_value=None),
        patch("gobby.cli.init._maybe_install_git_hooks_for_init"),
        patch("gobby.cli.init._maybe_run_linear_setup"),
    ):
        result = CliRunner().invoke(cli, ["init", "-C", str(project_dir)])

    assert result.exit_code == 0, result.output
    assert "Initialized project 'array-package'" in result.output
    project_json = project_dir / ".gobby" / "project.json"
    saved = json.loads(project_json.read_text(encoding="utf-8"))
    project = LocalProjectManager(hub_db).get(saved["id"])
    assert project is not None and project.name == "array-package"
    assert "verification" not in saved
    checkout = LocalProjectCheckoutManager(hub_db).get(machine_id, project.id)
    assert checkout is not None and checkout.root_path == str(project_dir.resolve())
