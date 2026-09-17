from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gobby.cli.hub_backup.files_home import (
    FilesHomeArchiveError,
    require_destination_files_home,
)


def test_require_destination_files_home_maps_windows_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "gobby-home"
    home.mkdir()
    (home / "bootstrap.yaml").write_text(
        "datastore_mode: local\nfiles_home: /nonexistent\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(FilesHomeArchiveError) as excinfo:
        require_destination_files_home()

    assert excinfo.value.code == "platform"
    assert "WSL 2" in str(excinfo.value)
