"""Repeat summaries follow SRT's canonical verdict on their message."""

from __future__ import annotations

import json
import selectors
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.agents.test_srt_monitor import MonitorHarness, monitor_harness

__all__ = ["monitor_harness"]

pytestmark = pytest.mark.unit

_SUFFIX = "_verdict_SBX"
# The filtered message is noise the canonical filter drops on its first event
# (see the stub in test_srt_monitor.py); its repeats must not be recorded.
_FILTERED = f"Sandbox: mDNSResponder(77) deny(1) mach-lookup com.apple.example {_SUFFIX}"
_FILE_WRITE = f"Sandbox: node(9) deny(1) file-write-create /tmp/denied {_SUFFIX}"
_NETWORK = f"Sandbox: node(9) deny(1) network-outbound 93.184.216.34:443 {_SUFFIX}"


def _records(journal: Path) -> list[dict[str, Any]]:
    if not journal.exists():
        return []
    return [json.loads(line) for line in journal.read_text().splitlines() if line]


def _count(records: list[dict[str, Any]], message: str) -> int:
    # A first event carries no `occurrences`; its summary carries the repeats.
    return sum(
        int(record.get("occurrences", 1)) for record in records if record.get("line") == message
    )


def _wait_for_journal(
    journal: Path, done: Callable[[list[dict[str, Any]]], bool]
) -> list[dict[str, Any]]:
    # Bounded poll, as `_wait_path` does for the monitor's backlog files.
    deadline = time.monotonic() + 15
    while True:
        records = _records(journal)
        if done(records):
            return records
        if time.monotonic() >= deadline:
            pytest.fail(f"journal did not reach the expected records: {records}")
        time.sleep(0.01)


def test_summary_follows_canonical_verdict_of_its_message(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    child = harness.launch(_SUFFIX)
    journal = harness.root / f"{_SUFFIX}.jsonl"
    try:
        assert child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=15)
            assert json.loads(child.stdout.readline()) is None
        # Filtered noise arrives first so its repeat summary flushes no later
        # than the accepted denials it precedes.
        for message, count in ((_FILTERED, 10), (_FILE_WRITE, 5), (_NETWORK, 3)):
            harness.events.write((json.dumps({"eventMessage": message}) + "\n") * count)
        harness.events.flush()

        records = _wait_for_journal(
            journal,
            lambda found: _count(found, _FILE_WRITE) == 5 and _count(found, _NETWORK) == 3,
        )

        # Real denials keep their first event and their summary with counts.
        summaries = [record for record in records if "occurrences" in record]
        assert {record["line"] for record in summaries} == {_FILE_WRITE, _NETWORK}
        for record in summaries:
            assert record["source"] == "srt-monitor"
            assert record["lastObserved"] >= record["firstObserved"]
        # The filtered message's repeat summary is never recorded.
        assert _count(records, _FILTERED) == 0
    finally:
        harness.finish()
        child.communicate(timeout=15)


def test_long_unique_events_do_not_evict_a_noise_verdict(
    monitor_harness: MonitorHarness,
) -> None:
    harness = monitor_harness
    child = harness.launch(_SUFFIX)
    journal = harness.root / f"{_SUFFIX}.jsonl"
    try:
        assert child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=15)
            assert json.loads(child.stdout.readline()) is None
        # Distinct events longer than the monitor's 8192-character repeat bound are
        # never repeated, so they must not push the noise's verdict out of the cache.
        padding = "x" * 8200
        harness.events.write(json.dumps({"eventMessage": _FILTERED}) + "\n")
        for index in range(257):
            long_message = (
                f"Sandbox: node(9) deny(1) file-write-create /tmp/long-{index} {padding} {_SUFFIX}"
            )
            harness.events.write(json.dumps({"eventMessage": long_message}) + "\n")
        harness.events.write((json.dumps({"eventMessage": _FILTERED}) + "\n") * 9)
        harness.events.write((json.dumps({"eventMessage": _FILE_WRITE}) + "\n") * 5)
        harness.events.flush()

        records = _wait_for_journal(journal, lambda found: _count(found, _FILE_WRITE) == 5)

        assert _count(records, _FILTERED) == 0
    finally:
        harness.finish()
        child.communicate(timeout=15)
