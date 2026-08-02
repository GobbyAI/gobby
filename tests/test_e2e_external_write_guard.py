"""Focused tests for the E2E real-home write guard."""

from __future__ import annotations

import os
from pathlib import Path

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
