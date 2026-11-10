"""Non-E2E checks for the worktree fixture's parent-index prerequisite."""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

from gobby.utils.native_bin import native_bin_name
from tests.e2e import test_worktrees_e2e as worktrees
from tests.e2e.conftest import DaemonInstance


@pytest.mark.unit
@pytest.mark.parametrize("index_fails", [False, True])
def test_worktree_fixture_indexes_registered_parent_with_isolated_gcode(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    index_fails: bool,
) -> None:
    project = tmp_path / "project"
    (project / ".gobby").mkdir(parents=True)
    (project / ".gobby" / "project.json").write_text('{"id":"test-project"}')
    daemon = Mock(spec=DaemonInstance)
    daemon.env = {
        "GOBBY_NATIVE_BIN_DIR": str(tmp_path / "isolated-bin"),
        "GOBBY_HOME": str(tmp_path / "isolated-home"),
        "HOME": str(tmp_path / "isolated-home"),
    }
    cli_events = Mock()
    registered = Mock()
    monkeypatch.setattr(worktrees, "wait_for_project_registration", registered)
    real_run = subprocess.run
    index_calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if Path(command[0]).name == native_bin_name("gcode"):
            assert registered.call_args.args == (daemon, "test-project")
            index_calls.append((command, kwargs))
            if index_fails:
                raise subprocess.CalledProcessError(1, command, stderr="index failed")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    # Pytest preserves the original callable, but omits it from its public typing.
    fixture = cast(
        Callable[[Path, pytest.TempPathFactory, Mock, Mock], Path],
        worktrees.git_repo_with_origin._get_wrapped_function(),
    )
    if index_fails:
        with pytest.raises(subprocess.CalledProcessError, match="non-zero exit status 1"):
            fixture(project, tmp_path_factory, cli_events, daemon)
    else:
        assert fixture(project, tmp_path_factory, cli_events, daemon) == project
    assert len(index_calls) == 1
    command, options = index_calls[0]
    assert command == [
        str(tmp_path / "isolated-bin" / native_bin_name("gcode")),
        "--project",
        str(project),
        "index",
    ]
    assert options["env"] == daemon.env
    assert options["cwd"] == str(project)
    assert options["check"] is True
    assert options["timeout"] == 60
