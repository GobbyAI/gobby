from __future__ import annotations

from pathlib import Path

import pytest

from gobby.hooks import _path_scope

pytestmark = pytest.mark.unit


def test_current_project_root_uses_project_path(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()

    assert _path_scope.current_project_root({"project_path": str(project_path)}) == project_path


def test_current_project_root_ignores_legacy_project_root_key(tmp_path: Path) -> None:
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()

    assert _path_scope.current_project_root({"project_root": str(legacy_root)}) is None


def test_current_project_root_falls_back_when_discovery_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()

    def raise_runtime_error(_cwd: Path) -> Path:
        raise RuntimeError("broken git discovery")

    monkeypatch.setattr(_path_scope, "find_project_root", raise_runtime_error)

    assert _path_scope.current_project_root({"cwd": str(cwd)}) is None
