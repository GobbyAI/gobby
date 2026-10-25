"""Shared fixtures for task tests."""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from gobby.code_index.gcode_gateway import GcodeCommandResult, GcodeGateway


@pytest.fixture(autouse=True)
def _isolated_evidence_snapshots() -> Iterator[None]:
    """Keep the per-session incremental derivation cache out of other tests."""
    from gobby.tasks.transcript_evidence_snapshots import clear_evidence_snapshots

    clear_evidence_snapshots()
    yield
    clear_evidence_snapshots()


@pytest.fixture(autouse=True)
def _no_landing_index_refresh() -> Iterator[None]:
    """Landings refresh the code index; no test may run the real gcode binary."""

    async def _skipped(*_args: object, **_kwargs: object) -> GcodeCommandResult:
        return GcodeCommandResult(
            command=("gcode", "index"),
            returncode=0,
            stdout="",
            stderr="",
            started_at="",
            completed_at="",
            duration_seconds=0.0,
            timeout_seconds=None,
        )

    with patch.object(GcodeGateway, "incremental_index", new=_skipped):
        yield
