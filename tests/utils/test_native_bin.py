from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.utils.native_bin import (
    local_native_bin_path,
    native_bin_name,
    resolve_native_bin,
    resolve_native_bin_or_default,
)

pytestmark = pytest.mark.unit


def test_native_bin_name_adds_windows_suffix() -> None:
    with patch("gobby.utils.native_bin.sys.platform", "win32"):
        assert native_bin_name("ghook") == "ghook.exe"
        assert native_bin_name("ghook.exe") == "ghook.exe"


def test_native_bin_name_is_unchanged_on_non_windows() -> None:
    with patch("gobby.utils.native_bin.sys.platform", "linux"):
        assert native_bin_name("ghook") == "ghook"
        assert native_bin_name("ghook.exe") == "ghook.exe"


def test_local_native_bin_path_prefers_gobby_home(temp_dir: Path) -> None:
    with patch.object(Path, "home", return_value=temp_dir):
        assert local_native_bin_path("ghook") == temp_dir / ".gobby" / "bin" / "ghook"


def test_resolve_native_bin_prefers_local_binary(temp_dir: Path) -> None:
    local_bin = temp_dir / ".gobby" / "bin" / "ghook"
    local_bin.parent.mkdir(parents=True)
    local_bin.write_text("")
    local_bin.chmod(0o755)

    with (
        patch.object(Path, "home", return_value=temp_dir),
        patch("gobby.utils.native_bin.shutil.which", return_value="/usr/local/bin/ghook"),
    ):
        assert resolve_native_bin("ghook") == str(local_bin)


def test_resolve_native_bin_ignores_non_executable_local_file(temp_dir: Path) -> None:
    local_bin = temp_dir / ".gobby" / "bin" / "ghook"
    local_bin.parent.mkdir(parents=True)
    local_bin.write_text("")
    local_bin.chmod(0o644)

    with (
        patch.object(Path, "home", return_value=temp_dir),
        patch("gobby.utils.native_bin.shutil.which", return_value="/usr/local/bin/ghook"),
        patch("gobby.utils.native_bin.sys.platform", "linux"),
    ):
        assert resolve_native_bin("ghook") == "/usr/local/bin/ghook"


def test_resolve_native_bin_falls_back_to_path(temp_dir: Path) -> None:
    with (
        patch.object(Path, "home", return_value=temp_dir),
        patch("gobby.utils.native_bin.shutil.which", return_value="/usr/local/bin/ghook"),
    ):
        assert resolve_native_bin("ghook") == "/usr/local/bin/ghook"


def test_resolve_native_bin_or_default_returns_name_when_missing(temp_dir: Path) -> None:
    with (
        patch.object(Path, "home", return_value=temp_dir),
        patch("gobby.utils.native_bin.shutil.which", return_value=None),
    ):
        assert resolve_native_bin_or_default("ghook") == "ghook"
