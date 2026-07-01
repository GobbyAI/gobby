from __future__ import annotations

import stat
from pathlib import Path

import pytest

from gobby.adapters.acp_filesystem import (
    ACPFileSystemError,
    resolve_file_path,
    write_text_file,
)

pytestmark = pytest.mark.unit


def test_resolve_file_path_rejects_nested_git_path(tmp_path: Path) -> None:
    target = tmp_path / "src" / ".git" / "config"

    with pytest.raises(ACPFileSystemError, match="access to .git paths is not allowed"):
        resolve_file_path(str(target), (tmp_path,))


def test_resolve_file_path_rejects_root_inside_git_dir(tmp_path: Path) -> None:
    root = tmp_path / ".git" / "worktree"
    root.mkdir(parents=True)
    target = root / "config"

    with pytest.raises(ACPFileSystemError, match="access to .git paths is not allowed"):
        resolve_file_path(str(target), (root,))


def test_write_text_file_preserves_existing_mode(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o640)

    written = write_text_file(str(target), (tmp_path,), content="new")

    assert written == 3
    assert target.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_write_text_file_preserves_write_error_when_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "notes.txt"

    def fail_replace(_src: str, _dst: Path) -> None:
        raise OSError("replace failed")

    def fail_unlink(_path: str) -> None:
        raise OSError("cleanup failed")

    monkeypatch.setattr("gobby.adapters.acp_filesystem.os.replace", fail_replace)
    monkeypatch.setattr("gobby.adapters.acp_filesystem.os.unlink", fail_unlink)

    with pytest.raises(ACPFileSystemError) as exc_info:
        write_text_file(str(target), (tmp_path,), content="new")

    assert str(exc_info.value) == "failed to write file"
    assert isinstance(exc_info.value.__cause__, OSError)
    assert str(exc_info.value.__cause__) == "replace failed"
