"""Focused acceptance tests for the generated code-index reindex hook body.

The codewiki curl refresh (and its authentication headers) was removed in
gobby-#19825; the body now only feeds changed files to the local gcode binary.
"""

import os
import subprocess
from pathlib import Path

import pytest

from gobby.cli.installers.git_hooks import _CODE_INDEX_REINDEX_BODY

pytestmark = pytest.mark.unit


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_hook_body(
    work_dir: Path,
    *,
    changed_files: str = "changed.py",
    gcode_available: bool = True,
    strict_unset: bool = False,
) -> list[str]:
    home = work_dir / "home"
    capture = work_dir / "gcode-args"

    if gcode_available:
        _write_executable(
            home / ".gobby/bin/gcode",
            '#!/bin/sh\nprintf "%s\\n" "$@" > "$GCODE_CAPTURE"\n',
        )

    env = os.environ | {
        "CHANGED_FILES": changed_files,
        "GCODE_CAPTURE": str(capture),
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
    }
    prelude = "set -u\n" if strict_unset else ""
    result = subprocess.run(
        ["/bin/bash", "-c", f"{prelude}{_CODE_INDEX_REINDEX_BODY}\nwait"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    if not capture.exists():
        return []
    return capture.read_text(encoding="utf-8").splitlines()


def test_hook_body_reindexes_changed_files(tmp_path: Path) -> None:
    args = _run_hook_body(tmp_path, changed_files="changed.py\nother.rs")

    assert args == ["index", "--quiet", "--skip-if-locked", "--files", "changed.py", "other.rs"]


def test_hook_body_skips_when_gcode_missing(tmp_path: Path) -> None:
    args = _run_hook_body(tmp_path, gcode_available=False)

    assert args == []


def test_hook_body_skips_without_changed_files(tmp_path: Path) -> None:
    args = _run_hook_body(tmp_path, changed_files="")

    assert args == []


def test_hook_body_survives_set_u(tmp_path: Path) -> None:
    """Chained user hooks run under set -u; the body must not abort."""
    args = _run_hook_body(tmp_path, strict_unset=True)

    assert args == ["index", "--quiet", "--skip-if-locked", "--files", "changed.py"]
