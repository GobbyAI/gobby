"""CLI uninstall coverage for Gobby-owned RTK fallback binaries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.uninstall import uninstall

pytestmark = pytest.mark.unit


def test_tools_flag_disables_rtk_rule_and_removes_owned_fallback(tmp_path: Path) -> None:
    binary = tmp_path / ".gobby" / "bin" / "rtk"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"owned")
    sidecar = binary.parent / ".rtk-gobby-install.json"
    sidecar.write_text(
        json.dumps(
            {
                "path": str(binary),
                "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "version": "0.45.0",
            }
        ),
        encoding="utf-8",
    )
    database = MagicMock()
    runtime = MagicMock()
    runtime.require_database.return_value = database
    impeccable_cleanup = MagicMock(removed=(), skipped=())

    with (
        patch("gobby.cli.uninstall.Path.home", return_value=tmp_path),
        patch("gobby.cli.uninstall.get_cli_runtime", return_value=runtime),
        patch("gobby.cli.uninstall.disable_rule_if_present") as disable,
        patch(
            "gobby.cli.uninstall.remove_impeccable_runtime",
            return_value=impeccable_cleanup,
        ),
    ):
        result = CliRunner().invoke(uninstall, ["--tools", "--yes"])

    assert result.exit_code == 0
    disable.assert_called_once_with(database)
    runtime.close.assert_called_once_with()
    assert not binary.exists()
    assert not sidecar.exists()
