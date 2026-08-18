from __future__ import annotations

import tempfile
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


def test_temp_scratchpad_accepts_cli_host_scratchpad_under_private_tmp() -> None:
    path = Path("/private/tmp/claude-501/-Users-josh-Projects-gobby/abc123/scratchpad/notes.md")

    assert _path_scope._is_temp_agent_scratchpad_path(path)


def test_temp_scratchpad_accepts_marker_under_tmp_root() -> None:
    path = Path("/tmp/claude-501/session/scratchpad/report.md").resolve(strict=False)

    assert _path_scope._is_temp_agent_scratchpad_path(path)


def test_temp_scratchpad_rejects_tmp_path_without_marker() -> None:
    path = Path("/private/tmp/claude-501/session/report.md")

    assert not _path_scope._is_temp_agent_scratchpad_path(path)


def test_temp_scratchpad_accepts_daemon_tempdir_marker() -> None:
    path = Path(tempfile.gettempdir()).resolve(strict=False) / "gobby-agent-scratchpad-x" / "out.md"

    assert _path_scope._is_temp_agent_scratchpad_path(path)


def test_temp_scratchpad_rejects_marker_paths_outside_temp_roots() -> None:
    path = Path("/opt/example/scratchpad/notes.md")

    assert not _path_scope._is_temp_agent_scratchpad_path(path)


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
