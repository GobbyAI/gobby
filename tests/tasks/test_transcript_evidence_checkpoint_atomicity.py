"""A checkpoint write cut short at any point leaves the prior checkpoint whole.

The pool drain no longer waits for an in-flight checkpoint write. That is safe only while
the write stays atomic: the payload lands in a temp file and replaces the checkpoint in one
rename, so an interrupted write never leaves a torn checkpoint behind.
"""

from __future__ import annotations

import os
import time

import pytest

from gobby.tasks import transcript_evidence_cache


def test_interrupted_checkpoint_write_keeps_the_prior_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript_evidence_cache.write_snapshot("session", b"prior")

    def interrupted_replace(source: str, target: str) -> None:
        # Stands in for a kill after the temp file is written and before the rename.
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", interrupted_replace)
    with pytest.raises(KeyboardInterrupt):
        transcript_evidence_cache.write_snapshot("session", b"in-flight")
    monkeypatch.undo()

    assert transcript_evidence_cache.read_snapshot("session") == b"prior"


def test_next_write_removes_a_stale_temp_left_by_a_killed_write() -> None:
    cache_dir = transcript_evidence_cache._snapshot_path("session").parent
    cache_dir.mkdir(parents=True, exist_ok=True)
    # A kill skips the cleanup in write_snapshot's finally block, so its temp file survives.
    stale = cache_dir / ".snapshot-killed"
    stale.write_bytes(b"partial")
    an_hour_ago = time.time() - 3600
    os.utime(stale, (an_hour_ago, an_hour_ago))
    # A temp that another process is still writing is recent and must stay.
    in_progress = cache_dir / ".snapshot-in-progress"
    in_progress.write_bytes(b"partial")

    transcript_evidence_cache.write_snapshot("session", b"next")

    assert not stale.exists()
    assert in_progress.exists()
    assert transcript_evidence_cache.read_snapshot("session") == b"next"
