"""Tests for workflows/sync.py — targeting uncovered lines."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.workflows.sync import (
    _ensure_gobby_tag_on_installed,
    sync_bundled_variables,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


class TestEnsureGobbyTag:
    def test_adds_gobby_tag(self) -> None:
        mgr = MagicMock()
        row = MagicMock()
        row.id = "r1"
        row.source = "template"
        row.tags = ["other"]
        mgr.list_all.return_value = [row]

        _ensure_gobby_tag_on_installed(mgr, "rule")
        mgr.update.assert_called_once_with("r1", tags=["other", "gobby"])

    def test_skips_when_tag_present(self) -> None:
        mgr = MagicMock()
        row = MagicMock()
        row.id = "r1"
        row.source = "template"
        row.tags = ["gobby", "other"]
        mgr.list_all.return_value = [row]

        _ensure_gobby_tag_on_installed(mgr, "rule")
        mgr.update.assert_not_called()

    def test_skips_non_template_sources(self) -> None:
        mgr = MagicMock()
        row = MagicMock()
        row.id = "r1"
        row.source = "project"
        row.tags = []
        mgr.list_all.return_value = [row]

        _ensure_gobby_tag_on_installed(mgr, "rule")
        mgr.update.assert_not_called()

    def test_handles_none_tags(self) -> None:
        mgr = MagicMock()
        row = MagicMock()
        row.id = "r1"
        row.source = "template"
        row.tags = None
        mgr.list_all.return_value = [row]

        _ensure_gobby_tag_on_installed(mgr, "rule")
        mgr.update.assert_called_once_with("r1", tags=["gobby"])


# ---------------------------------------------------------------------------
# sync_bundled_variables
# ---------------------------------------------------------------------------


class TestSyncBundledVariables:
    def test_path_not_exists(self) -> None:
        db = MagicMock()

        with patch(
            "gobby.workflows.sync.get_bundled_variables_path",
            return_value=Path("/nonexistent"),
        ):
            result = sync_bundled_variables(db)

        assert result["success"] is True
        assert result["synced"] == 0

    def test_create_new_variable(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            tags: [config]
            variables:
              my_var:
                value: true
                description: A test variable
        """)
        var_file = tmp_path / "vars.yaml"
        var_file.write_text(yaml_content)

        db = MagicMock()
        mgr = MagicMock()
        mgr.get_by_name.return_value = None
        mgr.list_all.return_value = []

        with (
            patch("gobby.workflows.sync.get_bundled_variables_path", return_value=tmp_path),
            patch("gobby.workflows.sync.LocalWorkflowDefinitionManager", return_value=mgr),
        ):
            db.fetchall.return_value = []
            result = sync_bundled_variables(db)

        assert result["synced"] == 1

    def test_skip_non_dict_variable(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            variables:
              bad_var: "just a string"
        """)
        var_file = tmp_path / "vars.yaml"
        var_file.write_text(yaml_content)

        db = MagicMock()
        mgr = MagicMock()
        mgr.list_all.return_value = []

        with (
            patch("gobby.workflows.sync.get_bundled_variables_path", return_value=tmp_path),
            patch("gobby.workflows.sync.LocalWorkflowDefinitionManager", return_value=mgr),
        ):
            db.fetchall.return_value = []
            result = sync_bundled_variables(db)

        assert "not a dict" in result["errors"][0]

    def test_orphan_cleanup_variables(self, tmp_path: Path) -> None:
        db = MagicMock()
        mgr = MagicMock()
        mgr.list_all.return_value = []

        db.fetchall.return_value = [
            {"id": "orphan-v1", "name": "removed-var"},
        ]

        with (
            patch("gobby.workflows.sync.get_bundled_variables_path", return_value=tmp_path),
            patch("gobby.workflows.sync.LocalWorkflowDefinitionManager", return_value=mgr),
        ):
            result = sync_bundled_variables(db)

        assert result["orphaned"] == 1


# ---------------------------------------------------------------------------
# _sync_single_variable
# ---------------------------------------------------------------------------
