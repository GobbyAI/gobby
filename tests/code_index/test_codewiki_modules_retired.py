"""Retirement checks for the daemon-owned CodeWiki modules."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

RETIRED_MODULES = ("codewiki_refresh", "codewiki_trigger", "codewiki_nightly")
IDENTIFIER_CHAR = r"[A-Za-z0-9_]"


def _retired_module_hits(root: Path, module: str) -> list[Path]:
    pattern = re.compile(rf"(?<!{IDENTIFIER_CHAR}){re.escape(module)}(?!{IDENTIFIER_CHAR})")
    return [
        path.relative_to(root) for path in root.rglob("*.py") if pattern.search(path.read_text())
    ]


def test_daemon_codewiki_modules_are_retired(repo_root: Path, tmp_path: Path) -> None:
    positive_control = tmp_path / "retired_import.py"
    positive_control.write_text("from gobby.code_index.codewiki_nightly import x\n")

    assert _retired_module_hits(tmp_path, "codewiki_nightly") == [Path("retired_import.py")]
    assert {
        module: _retired_module_hits(repo_root / "src", module) for module in RETIRED_MODULES
    } == {module: [] for module in RETIRED_MODULES}
