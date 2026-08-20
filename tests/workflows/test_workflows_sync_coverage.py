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
                "gobby.workflows.sync_variables.SessionVariableDefaultManager",
                return_value=mgr,
            ),
        ):
            result = sync_bundled_variables(db)

        assert result["success"] is True
        assert result["synced"] == 1
        assert mgr.create.call_args.kwargs["name"] == "my_var"

    def test_orphan_cleanup_is_skipped_without_existing_paths(self, tmp_path: Path) -> None:
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
                "gobby.workflows.sync_variables.SessionVariableDefaultManager",
                return_value=mgr,
            ),
        ):
            result = sync_bundled_variables(db)

        assert result["success"] is True
        assert result["orphaned"] == 0
        mgr.delete.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_sync_placeholders
# ---------------------------------------------------------------------------


class TestResolveSyncPlaceholders:
    def test_falls_back_to_sys_executable(self) -> None:
        with (
            patch("gobby.workflows.sync_rules.shutil.which", return_value=None),
            patch("gobby.workflows.sync_rules.sys.executable", "/home/user/.venv/bin/python3"),
        ):
            result = resolve_sync_placeholders('{"cmd": "{{ gobby_bin }} compress"}')
        assert result == '{"cmd": "/home/user/.venv/bin/python3 -m gobby compress"}'

    def test_multiple_occurrences_replaced(self) -> None:
        with patch("gobby.workflows.sync_rules.shutil.which", return_value="/bin/gobby"):
            result = resolve_sync_placeholders(
                '{"a": "{{ gobby_bin }} x", "b": "{{ gobby_bin }} y"}'
            )
        assert result == '{"a": "/bin/gobby x", "b": "/bin/gobby y"}'


# ---------------------------------------------------------------------------
# _sync_single_variable
# ---------------------------------------------------------------------------
