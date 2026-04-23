"""Tests for split workflow sync modules — targeting uncovered lines."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.workflows.sync_rules import resolve_sync_placeholders
from gobby.workflows.sync_variables import sync_bundled_variables

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# sync_bundled_variables
# ---------------------------------------------------------------------------


class TestSyncBundledVariables:
    def test_path_not_exists(self) -> None:
        db = MagicMock()

        with patch(
            "gobby.workflows.sync_variables.get_bundled_variables_path",
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
            patch(
                "gobby.workflows.sync_variables.get_bundled_variables_path", return_value=tmp_path
            ),
            patch(
                "gobby.workflows.sync_variables.LocalWorkflowDefinitionManager", return_value=mgr
            ),
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
            patch(
                "gobby.workflows.sync_variables.get_bundled_variables_path", return_value=tmp_path
            ),
            patch(
                "gobby.workflows.sync_variables.LocalWorkflowDefinitionManager", return_value=mgr
            ),
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
            patch(
                "gobby.workflows.sync_variables.get_bundled_variables_path", return_value=tmp_path
            ),
            patch(
                "gobby.workflows.sync_variables.LocalWorkflowDefinitionManager", return_value=mgr
            ),
        ):
            result = sync_bundled_variables(db)

        assert result["orphaned"] == 1


# ---------------------------------------------------------------------------
# resolve_sync_placeholders
# ---------------------------------------------------------------------------


class TestResolveSyncPlaceholders:
    def test_replaces_gobby_bin_with_which(self) -> None:
        with patch("gobby.workflows.sync_rules.shutil.which", return_value="/usr/local/bin/gobby"):
            result = resolve_sync_placeholders('{"cmd": "{{ gobby_bin }} compress -- foo"}')
        assert result == '{"cmd": "/usr/local/bin/gobby compress -- foo"}'

    def test_falls_back_to_sys_executable(self) -> None:
        with (
            patch("gobby.workflows.sync_rules.shutil.which", return_value=None),
            patch("gobby.workflows.sync_rules.sys.executable", "/home/user/.venv/bin/python3"),
        ):
            result = resolve_sync_placeholders('{"cmd": "{{ gobby_bin }} compress"}')
        assert result == '{"cmd": "/home/user/.venv/bin/python3 -m gobby compress"}'

    def test_no_placeholder_returns_unchanged(self) -> None:
        original = '{"cmd": "gobby compress -- foo"}'
        result = resolve_sync_placeholders(original)
        assert result == original

    def test_multiple_occurrences_replaced(self) -> None:
        with patch("gobby.workflows.sync_rules.shutil.which", return_value="/bin/gobby"):
            result = resolve_sync_placeholders(
                '{"a": "{{ gobby_bin }} x", "b": "{{ gobby_bin }} y"}'
            )
        assert result == '{"a": "/bin/gobby x", "b": "/bin/gobby y"}'


# ---------------------------------------------------------------------------
# _sync_single_variable
# ---------------------------------------------------------------------------
