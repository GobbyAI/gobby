import logging
from collections.abc import Callable
from functools import partial
from unittest.mock import patch

import pytest

from gobby.sessions import transcript_search_timing as timing

pytestmark = pytest.mark.unit


async def test_worker_timing_accepts_partial_and_uses_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=timing.__name__)
    with timing.transcript_search_timing():
        result = await timing.transcript_to_thread(partial(int, "42"))
    assert result == 42
    assert "phase=partial" in caplog.text
    assert all(record.levelno == logging.DEBUG for record in caplog.records)


async def test_resume_timing_excludes_worker_log_cost() -> None:
    clock = [0.0]
    records: list[str] = []

    def record(message: str, *args: object) -> None:
        records.append(message % args)
        if "event=worker_finished" in message:
            clock[0] += 7.0

    async def inline_worker(work: Callable[[], int]) -> int:
        return work()

    with (
        patch.object(timing, "perf_counter", side_effect=lambda: clock[0]),
        patch.object(timing.logger, "info", side_effect=record),
        patch.object(timing.logger, "debug", side_effect=record),
        patch(
            "gobby.sessions.transcript_search_timing.asyncio.to_thread", side_effect=inline_worker
        ),
        timing.transcript_search_timing(),
    ):
        assert await timing.transcript_to_thread(lambda: 42) == 42
    resumed = [record for record in records if "event=resumed" in record]
    assert len(resumed) == 1
    assert "resume_s=0.000000" in resumed[0]
