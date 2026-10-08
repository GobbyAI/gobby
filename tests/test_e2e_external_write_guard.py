"""Focused tests for the E2E real-home write guard."""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from collections.abc import Callable, Generator
from pathlib import Path
from typing import cast

import psutil
import pytest

from tests.e2e import conftest as e2e
from tests.e2e.conftest import _snapshot_dir


def _write(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_ordinary_files_are_recorded_by_relative_path(tmp_path: Path) -> None:
    _write(tmp_path / "config.json")
    _write(tmp_path / "logs" / "daemon.log")

    snapshot = _snapshot_dir(tmp_path)

    assert "config.json" in snapshot
    assert "logs/daemon.log" in snapshot


def test_worktrees_are_recorded_one_level_deep(tmp_path: Path) -> None:
    """A new worktree is still caught; its contents are never walked."""
    _write(tmp_path / "worktrees" / "gobby" / "task-1" / "src" / "runner.py")
    _write(tmp_path / "worktrees" / "gobby" / "task-1" / ".ruff_cache" / "entry")

    snapshot = _snapshot_dir(tmp_path)

    assert "worktrees/gobby" in snapshot
    assert not [key for key in snapshot if key.startswith("worktrees/gobby/")]


def test_excluded_and_exempt_directories_are_pruned(tmp_path: Path) -> None:
    _write(tmp_path / "skill-cache" / "cached.json")
    _write(tmp_path / "hooks" / "inbox" / "message.json")
    _write(tmp_path / "session_wiki" / "page.md")
    _write(tmp_path / "hooks" / "settings.json")

    snapshot = _snapshot_dir(tmp_path)

    assert "skill-cache/cached.json" not in snapshot
    assert "hooks/inbox/message.json" not in snapshot
    assert "session_wiki/page.md" not in snapshot
    assert "hooks/settings.json" in snapshot


def test_symlinked_directory_is_recorded_not_followed(tmp_path: Path) -> None:
    """A symlink appearing in ~/.gobby is itself the leak worth catching."""
    target = tmp_path / "outside"
    _write(target / "secret.token")
    (tmp_path / "linked").symlink_to(target, target_is_directory=True)

    snapshot = _snapshot_dir(tmp_path)

    assert "linked" in snapshot
    assert "linked/secret.token" not in snapshot


def test_missing_root_yields_empty_snapshot(tmp_path: Path) -> None:
    assert _snapshot_dir(tmp_path / "absent") == {}


def test_before_after_diff_detects_creation_and_modification(tmp_path: Path) -> None:
    """The guard's leak signal: a new key, or a changed mtime on an existing one."""
    _write(tmp_path / "config.json", "before")
    before = _snapshot_dir(tmp_path)

    _write(tmp_path / "escaped.db")
    (tmp_path / "config.json").write_text("after")
    os.utime(tmp_path / "config.json", (0, 0))
    after = _snapshot_dir(tmp_path)

    created = [key for key in after if key not in before]
    modified = [key for key in after if key in before and after[key] != before[key]]

    assert created == ["escaped.db"]
    assert modified == ["config.json"]


@pytest.mark.parametrize("foreign_writer", [True, False])
def test_recovery_write_is_attributed_to_its_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign_writer: bool
) -> None:
    """A concurrent seat and the test can write the exact same recovery path."""
    home = tmp_path / "real-home"
    recovery = home / ".gobby" / "recovery"
    recovery.mkdir(parents=True)
    path = recovery / "live-session.json"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(e2e, "_production_daemon_running", lambda: True)
    factory = cast(Callable[[], Generator[None]], inspect.unwrap(e2e.assert_no_external_writes))
    guard = factory()
    next(guard)
    if foreign_writer:
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('foreign')",
                str(path),
            ],
            check=True,
        )
        with pytest.raises(StopIteration):
            next(guard)
        assert path.read_text() == "foreign"
    else:
        path.write_text("test-owned")
        with pytest.raises(pytest.fail.Exception, match="recovery/live-session.json"):
            next(guard)


def test_readiness_bootstrap_records_isolated_daemon_same_path_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the real daemon bootstrap in another interpreter, without a server."""
    from tests.fixtures.external_write_audit import LOG_ENV, ROOT_ENV

    home = tmp_path / "real-home"
    recovery = home / ".gobby" / "recovery"
    recovery.mkdir(parents=True)
    target = recovery / "live-session.json"
    log = tmp_path / "owned-writes.jsonl"
    log.touch()
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    runner = tmp_path / "audit_writer.py"
    runner.write_text(
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['E2E_FAKE_TARGET']).write_text('daemon-owned')\n"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv(ROOT_ENV, str(home / ".gobby"))
    monkeypatch.setenv(LOG_ENV, str(log))
    env = dict(os.environ)
    env.update(
        GOBBY_TEST_PROTECT="1",
        GOBBY_HOME=str(isolated),
        E2E_FAKE_TARGET=str(target),
        PYTHONPATH=str(tmp_path),
    )
    owner = psutil.Process()
    env["GOBBY_E2E_OWNER_PID"] = str(owner.pid)
    env["GOBBY_E2E_OWNER_CREATE_TIME"] = str(owner.create_time())
    factory = cast(Callable[[], Generator[None]], inspect.unwrap(e2e.assert_no_external_writes))
    guard = factory()
    next(guard)
    bootstrap = Path(e2e.__file__).with_name("readiness_bootstrap.py")
    result = subprocess.run(
        [sys.executable, str(bootstrap), "audit_writer"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert target.read_text() == "daemon-owned"
    with pytest.raises(pytest.fail.Exception, match="recovery/live-session.json"):
        next(guard)
    assert "pid=" in log.read_text()


@pytest.mark.parametrize("operation", ["read", "modify", "unlink", "alias", "unlink_alias"])
def test_guard_checks_mutations_without_flagging_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    home = tmp_path / "real-home"
    path = home / ".gobby" / "recovery" / "session.json"
    _write(path, "existing")
    alias = tmp_path / "alias"
    alias.symlink_to(home / ".gobby", target_is_directory=True)
    if operation == "unlink_alias":
        outside = tmp_path / "outside.txt"
        outside.write_text("outside")
        path.unlink()
        path.symlink_to(outside)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    factory = cast(Callable[[], Generator[None]], inspect.unwrap(e2e.assert_no_external_writes))
    guard = factory()
    next(guard)
    if operation == "read":
        assert path.read_text() == "existing"
        with pytest.raises(StopIteration):
            next(guard)
        return
    if operation in {"unlink", "unlink_alias"}:
        path.unlink()
    elif operation == "alias":
        (alias / "recovery" / "session.json").write_text("modified through alias")
    else:
        path.write_text("modified")
    with pytest.raises(pytest.fail.Exception, match="recovery/session.json"):
        next(guard)
