"""Retention for the processed-envelope marker directory (#20851).

Every hook delivery writes one marker here and nothing used to remove them, so
the directory reached 1,745,433 files on the author's machine. At that size a
single marker write cost 22 ms and an exists() by name cost 159 us against a
~2 us floor, and because mark_envelope_processed runs on the daemon event loop
those costs were paid there -- which is what turned two cheap Path.is_file
frames into the dominant hot stacks in four separate loop-stall reports.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from gobby.hooks.envelope_dedupe import (
    ENVELOPE_REPLAY_GRACE_SECONDS,
    PROCESSED_MARKER_RETENTION_SECONDS,
    DirectoryPruneResult,
    mark_envelope_processed,
    prune_processed_envelope_markers,
)
from gobby.hooks.inbox import prune_hook_inbox

pytestmark = pytest.mark.unit

_DAY = 24 * 60 * 60.0


def _marker(processed_dir: Path, name: str, *, age_seconds: float) -> Path:
    """Write one marker file and backdate it to a chosen age."""
    processed_dir.mkdir(parents=True, exist_ok=True)
    path = processed_dir / f"{name}.json"
    path.write_text('{"status": "processed"}\n', encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_the_retention_window_clears_the_replay_grace_it_protects() -> None:
    """The window exists to outlive the last moment an envelope can return.

    Both readers of a marker -- inbox replay and duplicate-delivery response --
    are bounded by the replay grace, so the retention has to sit well above it
    or the prune would delete markers that are still load-bearing.
    """
    assert PROCESSED_MARKER_RETENTION_SECONDS > ENVELOPE_REPLAY_GRACE_SECONDS * 100


def test_a_marker_past_the_window_is_deleted_and_a_fresh_one_is_kept(
    tmp_path: Path,
) -> None:
    processed_dir = tmp_path / "processed"
    stale = _marker(processed_dir, "stale", age_seconds=2 * _DAY)
    fresh = _marker(processed_dir, "fresh", age_seconds=60.0)

    result = prune_processed_envelope_markers(processed_dir)

    assert result.deleted == 1
    assert result.examined == 2
    assert result.truncated is False
    assert not stale.exists()
    assert fresh.exists()


def test_a_marker_guarding_a_pending_envelope_survives_the_prune(
    tmp_path: Path,
) -> None:
    """Deleting this one would replay an envelope the daemon already handled.

    A pending inbox file is exactly the case the marker is for: the drain reads
    the marker to decide the envelope is done and drops the file. Age is what
    makes a marker safe to delete, never the absence of its envelope.
    """
    inbox_dir = tmp_path / "inbox"
    processed_dir = inbox_dir / "processed"
    envelope_id = "n-0000000000001-pending"
    inbox_dir.mkdir(parents=True)
    (inbox_dir / f"{envelope_id}.json").write_text("{}", encoding="utf-8")
    mark_envelope_processed(envelope_id, processed_dir=processed_dir)

    result = prune_processed_envelope_markers(processed_dir)

    assert result.deleted == 0
    assert list(processed_dir.iterdir())


def test_one_pass_stops_at_its_bound_and_the_next_pass_carries_on(
    tmp_path: Path,
) -> None:
    """The first pass after this shipped faced 1.7M entries and a 172s scan.

    Bounding the pass is what keeps that backlog from becoming one very long
    piece of work; it only pays off if the passes that follow resume it.
    """
    processed_dir = tmp_path / "processed"
    for index in range(10):
        _marker(processed_dir, f"stale-{index:02d}", age_seconds=2 * _DAY)

    first = prune_processed_envelope_markers(processed_dir, max_entries=4)

    assert first.examined == 4
    assert first.deleted == 4
    assert first.truncated is True
    assert len(list(processed_dir.iterdir())) == 6

    second = prune_processed_envelope_markers(processed_dir, max_entries=4)

    assert second.deleted == 4
    assert len(list(processed_dir.iterdir())) == 2


def test_a_pass_that_reads_the_whole_directory_is_not_reported_as_truncated(
    tmp_path: Path,
) -> None:
    """Steady state is ~27k entries against a 100k bound, so the ordinary pass
    finishes the directory and the backlog flag has to stay off for it."""
    processed_dir = tmp_path / "processed"
    for index in range(4):
        _marker(processed_dir, f"stale-{index}", age_seconds=2 * _DAY)

    result = prune_processed_envelope_markers(processed_dir, max_entries=4)

    assert result.deleted == 4
    assert result.truncated is False


def test_a_missing_marker_directory_is_not_an_error(tmp_path: Path) -> None:
    """The daemon must not lose its maintenance loop before the first hook."""
    result = prune_processed_envelope_markers(tmp_path / "never-created")

    assert result.deleted == 0
    assert result.examined == 0


def test_entries_the_prune_cannot_delete_do_not_end_the_pass(
    tmp_path: Path,
) -> None:
    """A subdirectory and an unreadable entry are skipped, and the stale marker
    sitting behind them is still collected."""
    processed_dir = tmp_path / "processed"
    nested = processed_dir / "nested"
    nested.mkdir(parents=True)
    stamp = time.time() - 2 * _DAY
    os.utime(nested, (stamp, stamp))
    stale = _marker(processed_dir, "stale", age_seconds=2 * _DAY)
    os.chmod(processed_dir, 0o500)
    try:
        result = prune_processed_envelope_markers(processed_dir)
    finally:
        os.chmod(processed_dir, 0o700)

    assert result.examined == 2
    assert nested.is_dir()
    # The read-only directory blocks the unlink; the pass reports it deleted
    # nothing rather than raising, and the entry is collected once writable.
    assert result.deleted == 0
    assert stale.exists()

    assert prune_processed_envelope_markers(processed_dir).deleted == 1


@pytest.mark.asyncio
async def test_the_prune_never_runs_on_the_event_loop_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass stats every entry it reads. On the loop that is the same defect
    the marker growth caused in the first place, just moved one frame out."""
    inbox_dir = tmp_path / "inbox"
    _marker(inbox_dir / "processed", "stale", age_seconds=2 * _DAY)
    monkeypatch.setattr("gobby.hooks.inbox.get_hook_inbox_dir", lambda: inbox_dir)

    prune_threads: list[int] = []
    real_prune = prune_processed_envelope_markers

    def _recording_prune(processed_dir: Path | None = None) -> DirectoryPruneResult:
        prune_threads.append(threading.get_ident())
        return real_prune(processed_dir)

    monkeypatch.setattr("gobby.hooks.inbox.prune_processed_envelope_markers", _recording_prune)
    loop_thread = threading.get_ident()

    deleted = await prune_hook_inbox()

    assert deleted == 1
    assert prune_threads, "the prune must have walked the marker directory"
    assert loop_thread not in prune_threads, (
        "marker pruning ran on the event loop thread; it must be offloaded"
    )
