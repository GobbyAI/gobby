"""Retention for the temp files a dead ghook leaves in the inbox (#20854).

ghook writes each envelope to `<name>.json.tmp` and renames it into place. Every
error return after the create leaves that temp behind, and so does a killed
process. `_iter_inbox_files` skips `*.tmp`, so an orphan is never replayed and
never deleted -- 59 had accumulated in `~/.gobby/hooks/inbox`, the oldest from
2026-04-26, in the same directory whose `processed/` sibling reached 1,745,433
entries before #20851 for the same reason.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from gobby.hooks.envelope_dedupe import DirectoryPruneResult
from gobby.hooks.inbox import (
    ORPHANED_TEMP_RETENTION_SECONDS,
    _iter_inbox_files,
    prune_hook_inbox,
    prune_orphaned_inbox_temp_files,
)

pytestmark = pytest.mark.unit

_HOUR = 60 * 60.0


def _inbox_file(inbox_dir: Path, name: str, *, age_seconds: float) -> Path:
    """Write one inbox file and backdate it to a chosen age."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    path = inbox_dir / name
    path.write_text('{"schema_version": 1}\n', encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_the_retention_window_outlasts_the_write_it_must_not_interrupt() -> None:
    """A ghook write is create, write, fsync, rename with no waiting between
    the steps, so the window only has to be far longer than one such write."""
    assert ORPHANED_TEMP_RETENTION_SECONDS >= _HOUR


def test_an_abandoned_temp_file_is_deleted_and_a_fresh_one_is_kept(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "inbox"
    stale = _inbox_file(inbox_dir, "n-1-stale.json.tmp", age_seconds=2 * _HOUR)
    in_flight = _inbox_file(inbox_dir, "n-2-writing.json.tmp", age_seconds=1.0)

    result = prune_orphaned_inbox_temp_files(inbox_dir)

    assert result.deleted == 1
    assert not stale.exists()
    assert in_flight.exists(), "a temp file a running ghook may still rename was deleted"


def test_a_pending_envelope_is_never_touched_by_the_temp_reaper(tmp_path: Path) -> None:
    """The reaper shares a directory with envelopes awaiting replay, and one of
    those can be far older than the window whenever the daemon was down."""
    inbox_dir = tmp_path / "inbox"
    pending = _inbox_file(inbox_dir, "n-3-pending.json", age_seconds=30 * 24 * _HOUR)

    result = prune_orphaned_inbox_temp_files(inbox_dir)

    assert result.deleted == 0
    assert pending.exists()
    assert _iter_inbox_files(inbox_dir) == [pending]


def test_one_pass_stops_at_its_bound_and_the_next_pass_carries_on(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "inbox"
    for index in range(6):
        _inbox_file(inbox_dir, f"n-{index}.json.tmp", age_seconds=2 * _HOUR)

    first = prune_orphaned_inbox_temp_files(inbox_dir, max_entries=4)

    assert first.examined == 4
    assert first.deleted == 4
    assert first.truncated is True

    second = prune_orphaned_inbox_temp_files(inbox_dir, max_entries=4)

    assert second.deleted == 2
    assert second.truncated is False
    assert list(inbox_dir.iterdir()) == []


def test_entries_that_are_not_files_do_not_count_against_the_deletion(tmp_path: Path) -> None:
    """`processed`, `quarantine`, and `failures` are siblings of the temp files
    and an mtime past the window says nothing about a directory."""
    inbox_dir = tmp_path / "inbox"
    subdir = inbox_dir / "processed"
    subdir.mkdir(parents=True)
    stamp = time.time() - 30 * 24 * _HOUR
    os.utime(subdir, (stamp, stamp))

    result = prune_orphaned_inbox_temp_files(inbox_dir)

    assert result.deleted == 0
    assert subdir.is_dir()


def test_a_missing_inbox_directory_is_not_an_error(tmp_path: Path) -> None:
    result = prune_orphaned_inbox_temp_files(tmp_path / "absent")

    assert result == DirectoryPruneResult()


@pytest.mark.asyncio
async def test_the_temp_reaper_never_runs_on_the_event_loop_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pass stats every entry it reads, which is the cost that put the
    marker directory on the loop's critical path in the first place."""
    inbox_dir = tmp_path / "inbox"
    _inbox_file(inbox_dir, "n-1-stale.json.tmp", age_seconds=2 * _HOUR)
    monkeypatch.setattr("gobby.hooks.inbox.get_hook_inbox_dir", lambda: inbox_dir)

    reaper_threads: list[int] = []
    real_reaper = prune_orphaned_inbox_temp_files

    def _recording_reaper(target: Path | None = None) -> DirectoryPruneResult:
        reaper_threads.append(threading.get_ident())
        return real_reaper(target)

    monkeypatch.setattr("gobby.hooks.inbox.prune_orphaned_inbox_temp_files", _recording_reaper)
    loop_thread = threading.get_ident()

    deleted = await prune_hook_inbox()

    assert deleted == 1
    assert reaper_threads, "the reaper must have walked the inbox directory"
    assert loop_thread not in reaper_threads, (
        "temp reaping ran on the event loop thread; it must be offloaded"
    )
