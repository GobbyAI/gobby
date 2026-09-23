"""Shared fixtures for task tests."""

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _isolated_evidence_snapshots() -> Iterator[None]:
    """Keep the per-session incremental derivation cache out of other tests."""
    from gobby.tasks.transcript_evidence import clear_evidence_snapshots

    clear_evidence_snapshots()
    yield
    clear_evidence_snapshots()
