"""Tests for CodeIndexTrigger end-of-tick post-edit batching."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.code_index.gcode_gateway import (
    GcodeCommandResult,
    GcodeDaemonConfigUnavailableError,
    GcodeGateway,
    GcodeUnavailableError,
)
from gobby.code_index.sync_breaker import BreakerState, SyncCircuitBreaker
from gobby.code_index.trigger import CodeIndexTrigger
from gobby.runtime_grants.launch import ManagedLaunch

pytestmark = pytest.mark.unit

DAEMON_CONFIG_STDERR = (
    "Error: daemon effective config unavailable "
    "(timeout; url=http://127.0.0.1:60887/api/config/effective)"
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _result(
    *,
    returncode: int | None = 0,
    stderr: str = "",
    timed_out: bool = False,
    timeout_seconds: float = 0.01,
) -> GcodeCommandResult:
    return GcodeCommandResult(
        command=("gcode", "index"),
        returncode=returncode,
        stdout="",
        stderr=stderr,
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:01+00:00",
        duration_seconds=1.0,
        timeout_seconds=timeout_seconds,
        timed_out=timed_out,
    )


class RecordingGateway(GcodeGateway):
    def __init__(self) -> None:
        self.calls: list[tuple[Path, tuple[str, ...], float | None]] = []
        self.envs: list[dict[str, str] | None] = []
        self.outcomes: deque[GcodeCommandResult | BaseException] = deque()

    async def incremental_index(
        self,
        project_root: Path,
        files: Sequence[str],
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GcodeCommandResult:
        self.calls.append((project_root, tuple(files), timeout))
        self.envs.append(dict(env) if env is not None else None)
        if not self.outcomes:
            return _result(timeout_seconds=timeout or 0.01)
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FirstCallBlockingGateway(RecordingGateway):
    def __init__(self) -> None:
        super().__init__()
        self.first_call_started = asyncio.Event()
        self.release_first_call = asyncio.Event()
        self.active_calls = 0
        self.max_active_calls = 0

    async def incremental_index(
        self,
        project_root: Path,
        files: Sequence[str],
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GcodeCommandResult:
        self.calls.append((project_root, tuple(files), timeout))
        self.envs.append(dict(env) if env is not None else None)
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            if len(self.calls) == 1:
                self.first_call_started.set()
                await self.release_first_call.wait()
            return _result(timeout_seconds=timeout or 0.01)
        finally:
            self.active_calls -= 1


@dataclass
class TriggerHarness:
    trigger: CodeIndexTrigger
    gateway: RecordingGateway
    breaker: SyncCircuitBreaker
    clock: FakeClock


@pytest.fixture
async def harness() -> TriggerHarness:
    clock = FakeClock()
    gateway = RecordingGateway()
    breaker = SyncCircuitBreaker(
        name="Gcode daemon-config",
        probe_target="daemon config endpoint",
        operation="daemon-owned gcode work",
        failure_threshold=1,
        base_backoff_seconds=30.0,
        max_backoff_seconds=900.0,
        monotonic=clock,
    )
    trigger = CodeIndexTrigger(
        loop=asyncio.get_running_loop(),
        retry_base_seconds=5.0,
        retry_max_seconds=10.0,
        index_timeout_seconds=0.01,
        gcode_gateway=gateway,
        daemon_config_breaker=breaker,
    )
    return TriggerHarness(trigger=trigger, gateway=gateway, breaker=breaker, clock=clock)


def _cancel_scheduled_callback(trigger: CodeIndexTrigger, root_key: str) -> None:
    callback = trigger._scheduled_by_root.pop(root_key)
    callback.cancel()


async def _wait_for_call_count(gateway: RecordingGateway, expected: int) -> None:
    for _ in range(20):
        if len(gateway.calls) >= expected:
            return
        await _next_loop_turn()
    pytest.fail(f"expected {expected} gateway calls, received {len(gateway.calls)}")


async def _wait_for_scheduled_callback(
    trigger: CodeIndexTrigger,
    root_key: str,
) -> asyncio.Handle:
    for _ in range(20):
        callback = trigger._scheduled_by_root.get(root_key)
        if callback is not None:
            return callback
        await _next_loop_turn()
    pytest.fail(f"expected a scheduled callback for {root_key}")


async def _next_loop_turn() -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    loop.call_soon(ready.set_result, None)
    await ready


@pytest.mark.asyncio
async def test_single_file_triggers_gateway_with_root_and_timeout(
    harness: TriggerHarness,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    file_path = root / "src" / "foo.py"

    harness.trigger.notify_file_changed(str(file_path), "proj-1", str(root))
    assert harness.gateway.calls == []
    await _wait_for_call_count(harness.gateway, 1)

    assert harness.gateway.calls == [(root.resolve(), ("src/foo.py",), 0.01)]
    assert harness.gateway.envs == [None]


@pytest.mark.asyncio
async def test_flush_resolves_launch_factory_from_launch_source(tmp_path: Path) -> None:
    @contextmanager
    def _dummy_launch(project_id: str, *, timeout_seconds: float) -> Iterator[ManagedLaunch]:
        del project_id, timeout_seconds
        yield ManagedLaunch(
            grant_path=Path("/tmp/grant.json"),
            env={"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/tmp/grant.json"},
        )

    class DummyLaunchFactory:
        def open(
            self, project_id: str, *, timeout_seconds: float
        ) -> AbstractContextManager[ManagedLaunch]:
            return _dummy_launch(project_id, timeout_seconds=timeout_seconds)

    class LaunchSource:
        launch_factory = DummyLaunchFactory()

    clock = FakeClock()
    gateway = RecordingGateway()
    breaker = SyncCircuitBreaker(
        name="Gcode daemon-config",
        probe_target="daemon config endpoint",
        operation="daemon-owned gcode work",
        failure_threshold=1,
        base_backoff_seconds=30.0,
        max_backoff_seconds=900.0,
        monotonic=clock,
    )
    trigger = CodeIndexTrigger(
        loop=asyncio.get_running_loop(),
        retry_base_seconds=5.0,
        retry_max_seconds=10.0,
        index_timeout_seconds=0.01,
        gcode_gateway=gateway,
        daemon_config_breaker=breaker,
        launch_source=cast(Any, LaunchSource()),
    )
    root = tmp_path / "repo"
    root.mkdir()
    trigger.notify_file_changed(str(root / "src" / "foo.py"), "proj-1", str(root))
    await _wait_for_call_count(gateway, 1)

    assert gateway.envs == [{"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/tmp/grant.json"}]


@pytest.mark.asyncio
async def test_multiple_files_are_sorted_and_batched(
    harness: TriggerHarness,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    for file_path in ("src/c.py", "src/a.py", "src/b.py", "src/a.py"):
        harness.trigger.notify_file_changed(file_path, "proj-1", str(root))

    await _wait_for_call_count(harness.gateway, 1)
    await _next_loop_turn()

    assert harness.gateway.calls == [(root.resolve(), ("src/a.py", "src/b.py", "src/c.py"), 0.01)]


@pytest.mark.asyncio
async def test_same_file_is_deduplicated(harness: TriggerHarness, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    for _ in range(3):
        harness.trigger._schedule_file("src/foo.py", "proj-1", str(root))

    await harness.trigger._flush(harness.trigger._root_key(str(root)), "proj-1")

    assert harness.gateway.calls[0][1] == ("src/foo.py",)


@pytest.mark.asyncio
async def test_same_turn_files_share_one_scheduled_callback(
    harness: TriggerHarness,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    root_key = harness.trigger._root_key(str(root))
    harness.trigger._schedule_file("src/a.py", "proj-1", str(root))
    first_callback = harness.trigger._scheduled_by_root[root_key]

    harness.trigger._schedule_file("src/b.py", "proj-1", str(root))
    second_callback = harness.trigger._scheduled_by_root[root_key]

    assert second_callback is first_callback
    assert not first_callback.cancelled()
    assert harness.gateway.calls == []

    await _wait_for_call_count(harness.gateway, 1)
    assert harness.gateway.calls[0][1] == ("src/a.py", "src/b.py")


@pytest.mark.asyncio
async def test_different_roots_flush_independently(
    harness: TriggerHarness,
    tmp_path: Path,
) -> None:
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    harness.trigger.notify_file_changed("src/shared.py", "parent-proj", str(root_a))
    harness.trigger.notify_file_changed("src/shared.py", "parent-proj", str(root_b))

    await _wait_for_call_count(harness.gateway, 2)

    assert {call[0] for call in harness.gateway.calls} == {root_a.resolve(), root_b.resolve()}


@pytest.mark.asyncio
async def test_edits_during_active_run_form_one_serial_follow_up_batch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    gateway = FirstCallBlockingGateway()
    trigger = CodeIndexTrigger(
        loop=asyncio.get_running_loop(),
        gcode_gateway=gateway,
        daemon_config_breaker=SyncCircuitBreaker(
            name="test",
            probe_target="daemon config",
            operation="index",
            failure_threshold=1,
        ),
    )

    trigger.notify_file_changed("src/a.py", "proj-1", str(root))
    await asyncio.wait_for(gateway.first_call_started.wait(), timeout=1.0)
    for file_path in ("src/c.py", "src/b.py", "src/b.py"):
        trigger.notify_file_changed(file_path, "proj-1", str(root))
    await _next_loop_turn()
    await _next_loop_turn()

    assert len(gateway.calls) == 1
    gateway.release_first_call.set()
    await _wait_for_call_count(gateway, 2)

    assert [call[1] for call in gateway.calls] == [("src/a.py",), ("src/b.py", "src/c.py")]
    assert gateway.max_active_calls == 1


@pytest.mark.asyncio
async def test_pending_paths_resolve_under_root(
    harness: TriggerHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    daemon_cwd = tmp_path / "daemon-cwd"
    root.mkdir()
    daemon_cwd.mkdir()
    monkeypatch.chdir(daemon_cwd)

    harness.trigger._schedule_file("src/pkg.py", "proj-1", str(root))
    await harness.trigger._flush(harness.trigger._root_key(str(root)), "proj-1")

    assert harness.gateway.calls == [(root.resolve(), ("src/pkg.py",), 0.01)]


@pytest.mark.asyncio
async def test_command_failure_requeues_with_bounded_backoff(
    harness: TriggerHarness,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root_key = harness.trigger._root_key(str(tmp_path))
    harness.trigger._pending_by_root[root_key] = {"src/foo.py"}
    harness.gateway.outcomes.extend(
        [
            _result(returncode=1, stderr="bad index"),
            _result(returncode=1, stderr="bad index"),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.trigger"):
        await harness.trigger._flush(root_key, "proj-1")
        _cancel_scheduled_callback(harness.trigger, root_key)
        await harness.trigger._flush(root_key, "proj-1")
        _cancel_scheduled_callback(harness.trigger, root_key)

    assert harness.trigger._pending_by_root[root_key] == {"src/foo.py"}
    assert harness.trigger._retry_delay_by_root[root_key] == 10.0
    assert caplog.text.count("gcode index exited 1: bad index") == 2


@pytest.mark.asyncio
async def test_lock_busy_requeues_without_warning_and_closes_breaker(
    harness: TriggerHarness,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root_key = harness.trigger._root_key(str(tmp_path))
    harness.trigger._pending_by_root[root_key] = {"src/foo.py"}
    harness.breaker.record_failure()
    caplog.clear()
    harness.clock.now = 30.0
    harness.gateway.outcomes.append(_result(returncode=3, stderr="index lock busy"))

    with caplog.at_level(logging.INFO):
        await harness.trigger._flush(root_key, "proj-1")
    _cancel_scheduled_callback(harness.trigger, root_key)

    assert harness.trigger._pending_by_root[root_key] == {"src/foo.py"}
    assert harness.trigger._retry_delay_by_root[root_key] == 10.0
    assert harness.breaker.state is BreakerState.CLOSED
    assert "breaker closed" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_timeout_result_requeues_with_command_backoff(
    harness: TriggerHarness,
    tmp_path: Path,
) -> None:
    root_key = harness.trigger._root_key(str(tmp_path))
    harness.trigger._pending_by_root[root_key] = {"src/foo.py"}
    harness.gateway.outcomes.append(
        _result(
            returncode=None,
            stderr="gcode timed out after 0.01s",
            timed_out=True,
        )
    )

    await harness.trigger._flush(root_key, "proj-1")
    _cancel_scheduled_callback(harness.trigger, root_key)

    assert harness.trigger._pending_by_root[root_key] == {"src/foo.py"}
    assert harness.trigger._retry_delay_by_root[root_key] == 10.0


@pytest.mark.asyncio
async def test_edits_during_retry_backoff_preserve_scheduled_delay(
    harness: TriggerHarness,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    root_key = harness.trigger._root_key(str(root))
    harness.gateway.outcomes.append(_result(returncode=1, stderr="bad index"))

    harness.trigger.notify_file_changed("src/a.py", "proj-1", str(root))
    await _wait_for_call_count(harness.gateway, 1)
    first_callback = await _wait_for_scheduled_callback(harness.trigger, root_key)
    assert isinstance(first_callback, asyncio.TimerHandle)
    scheduled_for = first_callback.when()

    harness.trigger.notify_file_changed("src/b.py", "proj-1", str(root))
    await _next_loop_turn()
    await _next_loop_turn()

    assert harness.trigger._scheduled_by_root[root_key] is first_callback
    assert first_callback.when() == scheduled_for
    assert harness.trigger._pending_by_root[root_key] == {"src/a.py", "src/b.py"}
    assert len(harness.gateway.calls) == 1
    _cancel_scheduled_callback(harness.trigger, root_key)


@pytest.mark.asyncio
async def test_daemon_config_failure_opens_shared_breaker_and_preserves_batch(
    harness: TriggerHarness,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root_key = harness.trigger._root_key(str(tmp_path))
    files = {"src/a.py", "src/b.py"}
    harness.trigger._pending_by_root[root_key] = set(files)
    harness.gateway.outcomes.append(
        GcodeDaemonConfigUnavailableError(("gcode", "index"), 1, DAEMON_CONFIG_STDERR)
    )

    with caplog.at_level(logging.WARNING):
        await harness.trigger._flush(root_key, "proj-1")

    callback = harness.trigger._scheduled_by_root[root_key]
    assert isinstance(callback, asyncio.TimerHandle)
    scheduled_delay = callback.when() - asyncio.get_running_loop().time()
    _cancel_scheduled_callback(harness.trigger, root_key)

    assert harness.breaker.state is BreakerState.OPEN
    assert harness.trigger._pending_by_root[root_key] == files
    assert scheduled_delay == pytest.approx(30.0, abs=0.1)
    assert len(harness.gateway.calls) == 1
    assert caplog.text.count("breaker open") == 1
    assert "gcode index exited 1" not in caplog.text
    assert DAEMON_CONFIG_STDERR not in caplog.text

    await harness.trigger._flush(root_key, "proj-1")
    _cancel_scheduled_callback(harness.trigger, root_key)
    assert len(harness.gateway.calls) == 1
    assert harness.trigger._pending_by_root[root_key] == files


@pytest.mark.asyncio
async def test_half_open_success_resumes_complete_batch(
    harness: TriggerHarness,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root_key = harness.trigger._root_key(str(tmp_path))
    files = {"src/a.py", "src/b.py"}
    harness.trigger._pending_by_root[root_key] = set(files)
    harness.gateway.outcomes.append(
        GcodeDaemonConfigUnavailableError(("gcode", "index"), 1, DAEMON_CONFIG_STDERR)
    )
    await harness.trigger._flush(root_key, "proj-1")
    _cancel_scheduled_callback(harness.trigger, root_key)
    harness.clock.now = 30.0

    with caplog.at_level(logging.INFO):
        await harness.trigger._flush(root_key, "proj-1")

    assert harness.gateway.calls[-1][1] == ("src/a.py", "src/b.py")
    assert root_key not in harness.trigger._pending_by_root
    assert harness.breaker.state is BreakerState.CLOSED
    assert "breaker closed" in caplog.text


@pytest.mark.asyncio
async def test_unavailable_gateway_requeues_and_warns(
    harness: TriggerHarness,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root_key = harness.trigger._root_key(str(tmp_path))
    harness.trigger._pending_by_root[root_key] = {"src/foo.py"}
    harness.gateway.outcomes.append(GcodeUnavailableError("gcode is not installed"))

    with caplog.at_level(logging.WARNING, logger="gobby.code_index.trigger"):
        await harness.trigger._flush(root_key, "proj-1")
    _cancel_scheduled_callback(harness.trigger, root_key)

    assert harness.trigger._pending_by_root[root_key] == {"src/foo.py"}
    assert "gcode index failed: gcode is not installed" in caplog.text


@pytest.mark.asyncio
async def test_empty_flush_is_noop(harness: TriggerHarness, tmp_path: Path) -> None:
    await harness.trigger._flush(str(tmp_path.resolve()), "nonexistent-project")
    assert harness.gateway.calls == []


@pytest.mark.asyncio
async def test_flush_propagates_gateway_cancellation(
    harness: TriggerHarness,
    tmp_path: Path,
) -> None:
    root_key = harness.trigger._root_key(str(tmp_path))
    harness.trigger._pending_by_root[root_key] = {"src/foo.py"}
    harness.gateway.outcomes.append(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await harness.trigger._flush(root_key, "proj-1")
