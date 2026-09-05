from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

import pytest

from gobby.providers.usage import (
    AgyUsageReporter,
    TransientUsageRefreshError,
    UsageWindow,
)

_CAPTURES = (
    Path(__file__).parents[1] / "fixtures" / "provider_contracts" / "agy" / "command-captures.json"
)
_OBSERVED_AT = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


class _UsageBucket(TypedDict):
    name: str
    remaining_fraction: float
    reset_time: str


class _UsageGroup(TypedDict):
    name: str
    buckets: list[_UsageBucket]


class _UsageData(TypedDict):
    groups: list[_UsageGroup]


class _UsageCommand(TypedDict):
    name: str
    data: _UsageData


class _UsageEnvelope(TypedDict):
    status: str
    num_turns: int
    usage: dict[str, int]
    command: _UsageCommand


class _Capture(TypedDict):
    record: str
    exit_code: int
    stdout: _UsageEnvelope
    stderr_tail: str


class _CaptureDocument(TypedDict):
    commands: list[_Capture]


def _capture(record: str) -> _Capture:
    document = cast(
        _CaptureDocument,
        json.loads(_CAPTURES.read_text(encoding="utf-8")),
    )
    return next(capture for capture in document["commands"] if capture["record"] == record)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [
        pytest.param("1.1.19 /usage (70% remaining)", id="healthy"),
        pytest.param(
            "1.1.19 /usage after exhaustion (remaining_fraction 0)",
            id="exhausted",
        ),
    ],
)
async def test_agy_usage_reporter_parses_recorded_snapshots(record: str) -> None:
    capture = _capture(record)
    commands: list[tuple[str, ...]] = []

    async def run_command(command: tuple[str, ...]) -> tuple[int, str, str]:
        commands.append(command)
        return (
            capture["exit_code"],
            json.dumps(capture["stdout"]),
            capture["stderr_tail"],
        )

    reporter = AgyUsageReporter(run_command=run_command, clock=lambda: _OBSERVED_AT)

    snapshot = await reporter.report()

    assert commands == [("agy", "-p", "/usage", "--output-format", "json")]
    assert snapshot.provider == "agy"
    assert snapshot.observed_at == _OBSERVED_AT
    assert snapshot.supported is True
    assert snapshot.raw == capture["stdout"]
    assert snapshot.windows == tuple(
        UsageWindow(
            label=f"{group['name']} — {bucket['name']}",
            used=1.0 - bucket["remaining_fraction"],
            limit=1.0,
            unit="fraction",
            resets_at=bucket["reset_time"],
        )
        for group in capture["stdout"]["command"]["data"]["groups"]
        for bucket in group["buckets"]
    )
    assert capture["stdout"]["num_turns"] == 0
    assert sum(capture["stdout"]["usage"].values()) == 0
    assert all("/credits" not in argument for command in commands for argument in command)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agy_usage_reporter_translates_nonzero_exit() -> None:
    async def run_command(_command: tuple[str, ...]) -> tuple[int, str, str]:
        return (1, "", "Please sign in")

    reporter = AgyUsageReporter(run_command=run_command)

    with pytest.raises(TransientUsageRefreshError) as raised:
        await reporter.report()

    assert raised.value.code == "command_failed"
    assert raised.value.reason == "Please sign in"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agy_usage_reporter_translates_timeout() -> None:
    blocker = asyncio.Event()

    async def run_command(_command: tuple[str, ...]) -> tuple[int, str, str]:
        await blocker.wait()
        return (0, "", "")

    reporter = AgyUsageReporter(run_command=run_command, timeout_seconds=0.001)

    with pytest.raises(TransientUsageRefreshError) as raised:
        await reporter.report()

    assert raised.value.code == "timeout"
    assert raised.value.reason == "timed out after 0.001 seconds"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agy_usage_reporter_translates_malformed_output() -> None:
    async def run_command(_command: tuple[str, ...]) -> tuple[int, str, str]:
        return (0, "{}", "")

    reporter = AgyUsageReporter(run_command=run_command)

    with pytest.raises(TransientUsageRefreshError) as raised:
        await reporter.report()

    assert raised.value.code == "invalid_payload"
    assert raised.value.reason == "command envelope status is not SUCCESS"
