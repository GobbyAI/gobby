"""Tests for the agent process-tree memory watchdog (incident #18196)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psutil
import pytest

from gobby.agents.memory_watchdog import MemoryWatchdogHandler
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun
from gobby.storage.terminals import Terminal
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.services import TerminalServices
from tests.terminals.fakes import FakeRuntime, MemoryTerminalStore, make_memory_terminal


class _WatchdogRuntime(FakeRuntime):
    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        await super().terminate(terminal, grace_seconds)
        self.killed_ids.add(terminal.id)


class _StickyRuntime(FakeRuntime):
    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        del grace_seconds
        name = terminal.session_name or terminal.spawn_key
        if name is not None:
            self.killed.append(name)


GB = 1024**3
OLD_TS = "2020-01-01T00:00:00+00:00"


class FakeProc:
    def __init__(
        self,
        pid: int,
        rss: int,
        name: str = "python3.14",
        cmdline: list[str] | None = None,
        children: list[FakeProc] | None = None,
        start_time: float = 1000.0,
    ) -> None:
        self.pid = pid
        self._rss = rss
        self._name = name
        self._cmdline = cmdline or ["uv", "run", "gobby", "mcp-server"]
        self.child_procs = children or []
        self._start_time = start_time

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list[str]:
        return self._cmdline

    def memory_info(self) -> Any:
        return SimpleNamespace(rss=self._rss)

    def create_time(self) -> float:
        return self._start_time


def make_run(
    run_id: str = "run-1",
    *,
    terminal_id: str | None = "gobby-test",
    started_at: str | None = OLD_TS,
) -> AgentRun:
    return AgentRun(
        id=run_id,
        parent_session_id="parent-session",
        provider="codex",
        prompt="test",
        status="running",
        created_at=OLD_TS,
        updated_at=OLD_TS,
        terminal_id=terminal_id,
        started_at=started_at,
    )


def make_handler(
    *,
    runs: list[AgentRun],
    trees: dict[int, FakeProc],
    process_pids: dict[str, int | None] | None = None,
    config: TmuxConfig | None = None,
    virtual_memory: Any | None = None,
    process_iter: Any | None = None,
    monotonic_values: list[float] | None = None,
    kill_succeeds: bool = True,
) -> tuple[MemoryWatchdogHandler, dict[str, Any]]:
    config = config or TmuxConfig()
    agent_run_manager = MagicMock()
    agent_run_manager.list_active_for_machine.return_value = runs
    agent_run_manager.clear_live_terminal = MagicMock()
    runs_by_id = {run.id: run for run in runs}
    agent_run_manager.get.side_effect = runs_by_id.get
    agent_run_manager.record_termination_intent.side_effect = (
        lambda run_id, **_kwargs: runs_by_id.get(run_id)
    )
    agent_run_manager.replace_capture_slot.side_effect = lambda run_id, **_kwargs: runs_by_id.get(
        run_id
    )

    process_pid_map = process_pids if process_pids is not None else {"gobby-test": 100}

    cleanup_handler = MagicMock()
    cleanup_handler.cleanup_agent = AsyncMock()

    def live_processes() -> dict[int, tuple[FakeProc, int]]:
        # Tests swap trees between ticks, so read them on every call.
        found: dict[int, tuple[FakeProc, int]] = {}
        stack = [(root, 1) for root in trees.values()]
        while stack:
            proc, ppid = stack.pop()
            found[proc.pid] = (proc, ppid)
            stack.extend((child, proc.pid) for child in proc.child_procs)
        return found

    def process_factory(pid: int) -> FakeProc:
        entry = live_processes().get(pid)
        if entry is None:
            raise psutil.NoSuchProcess(pid)
        return entry[0]

    ppid_scans: list[int] = []

    def ppid_map() -> dict[int, int]:
        ppids = {pid: ppid for pid, (_, ppid) in live_processes().items()}
        ppid_scans.append(len(ppids))
        return ppids

    vm = virtual_memory or SimpleNamespace(total=128 * GB, available=100 * GB)
    ticks = iter(monotonic_values or [float(i) * 1000 for i in range(100)])

    store = MemoryTerminalStore()
    for run in runs:
        if run.terminal_id:
            row = make_memory_terminal(
                terminal_id=run.terminal_id,
                backend="native",
            )
            row.process = {
                "host_terminal_id": run.terminal_id,
                "pgid": process_pid_map.get(run.terminal_id),
                "start_time": 1000.0,
            }
            row.host_epoch = "epoch"
            store.rows[row.id] = row
    runtime: FakeRuntime = (
        _WatchdogRuntime(backend="native") if kill_succeeds else _StickyRuntime(backend="native")
    )
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    handler = MemoryWatchdogHandler(
        agent_run_manager=agent_run_manager,
        db=MagicMock(),
        cleanup_handler=cleanup_handler,
        tmux_config=config,
        process_factory=process_factory,
        ppid_map_fn=ppid_map,
        virtual_memory_fn=lambda: vm,
        process_iter_fn=process_iter or (lambda attrs: []),
        monotonic=lambda: next(ticks),
        terminal_services=TerminalServices(manager=store, registry=registry),
    )
    mocks = {
        "agent_run_manager": agent_run_manager,
        "cleanup_handler": cleanup_handler,
        "runtime": runtime,
        "ppid_scans": ppid_scans,
    }
    return handler, mocks


@pytest.mark.asyncio
async def test_under_limit_no_action() -> None:
    handler, mocks = make_handler(
        runs=[make_run()],
        trees={100: FakeProc(100, rss=1 * GB)},
    )
    killed = await handler.check_agent_memory()
    assert killed == 0
    assert mocks["runtime"].killed_host_ids == []
    assert handler._breach_counts == {}


@pytest.mark.asyncio
async def test_kill_after_consecutive_breaches() -> None:
    tree = FakeProc(100, rss=1 * GB, children=[FakeProc(101, rss=20 * GB, name="python3.14")])
    handler, mocks = make_handler(runs=[make_run()], trees={100: tree})

    assert await handler.check_agent_memory() == 0
    assert mocks["runtime"].killed_host_ids == []
    assert handler._breach_counts["run-1"] == 1

    assert await handler.check_agent_memory() == 1
    assert mocks["runtime"].killed_host_ids == ["gobby-test"]
    mocks["agent_run_manager"].record_termination_intent.assert_called_once()
    mocks["agent_run_manager"].clear_live_terminal.assert_not_called()
    payload = mocks["cleanup_handler"].cleanup_agent.await_args.kwargs["terminal_payload"]
    assert "exceeded memory limit" in payload
    assert "pid=101" in payload
    assert "cmd=uv run gobby mcp-server" in payload
    assert handler._breach_counts == {}


@pytest.mark.asyncio
async def test_breach_counter_resets_when_back_under_limit() -> None:
    big = FakeProc(100, rss=20 * GB)
    small = FakeProc(100, rss=1 * GB)
    trees = {100: big}
    handler, mocks = make_handler(runs=[make_run()], trees=trees)

    assert await handler.check_agent_memory() == 0
    trees[100] = small
    assert await handler.check_agent_memory() == 0
    trees[100] = big
    assert await handler.check_agent_memory() == 0
    assert mocks["runtime"].killed_host_ids == []


@pytest.mark.asyncio
async def test_grace_period_skips_young_runs() -> None:
    recent = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    handler, mocks = make_handler(
        runs=[make_run(started_at=recent)],
        trees={100: FakeProc(100, rss=20 * GB)},
    )
    assert await handler.check_agent_memory() == 0
    assert await handler.check_agent_memory() == 0
    assert mocks["runtime"].killed_host_ids == []


@pytest.mark.asyncio
async def test_warn_only_mode_never_kills(caplog: pytest.LogCaptureFixture) -> None:
    config = TmuxConfig(memory_watchdog_action="warn")
    handler, mocks = make_handler(
        runs=[make_run()],
        trees={100: FakeProc(100, rss=20 * GB)},
        config=config,
    )
    with caplog.at_level(logging.WARNING):
        await handler.check_agent_memory()
        killed = await handler.check_agent_memory()
    assert killed == 0
    assert mocks["runtime"].killed_host_ids == []
    mocks["cleanup_handler"].cleanup_agent.assert_not_awaited()
    assert any("warn-only" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_disabled_watchdog_is_inert() -> None:
    config = TmuxConfig(memory_watchdog_enabled=False)
    handler, mocks = make_handler(
        runs=[make_run()],
        trees={100: FakeProc(100, rss=50 * GB)},
        config=config,
    )
    assert await handler.check_agent_memory() == 0
    mocks["agent_run_manager"].list_active_for_machine.assert_not_called()


@pytest.mark.asyncio
async def test_missing_native_process_id_and_dead_process_tolerated() -> None:
    runs = [
        make_run("run-1", terminal_id="gobby-a"),
        make_run("run-2", terminal_id="gobby-b"),
        make_run("run-3", terminal_id=None),
    ]
    # run-1 has no recorded process id; run-2's pid raises NoSuchProcess in the factory.
    handler, mocks = make_handler(
        runs=runs,
        trees={},
        process_pids={"gobby-a": None, "gobby-b": 200},
    )
    assert await handler.check_agent_memory() == 0
    assert mocks["runtime"].killed_host_ids == []


async def test_recycled_native_process_is_not_sampled() -> None:
    handler, mocks = make_handler(
        runs=[make_run()],
        trees={100: FakeProc(100, rss=20 * GB, start_time=2000.0)},
    )
    assert await handler.check_agent_memory() == 0
    assert await handler.check_agent_memory() == 0
    assert mocks["runtime"].killed_host_ids == []


@pytest.mark.asyncio
async def test_aggregate_budget_kills_largest_tree_only() -> None:
    runs = [
        make_run("run-1", terminal_id="gobby-a"),
        make_run("run-2", terminal_id="gobby-b"),
    ]
    config = TmuxConfig(agent_memory_limit_gb=16.0, agent_memory_total_limit_gb=10.0)
    handler, mocks = make_handler(
        runs=runs,
        trees={100: FakeProc(100, rss=8 * GB), 200: FakeProc(200, rss=6 * GB)},
        process_pids={"gobby-a": 100, "gobby-b": 200},
        config=config,
    )
    assert await handler.check_agent_memory() == 0  # breach 1/2
    assert await handler.check_agent_memory() == 1  # kills largest
    assert mocks["runtime"].killed_host_ids == ["gobby-a"]
    payload = mocks["cleanup_handler"].cleanup_agent.await_args.kwargs["terminal_payload"]
    assert "Aggregate agent memory exceeded budget" in payload


@pytest.mark.asyncio
async def test_aggregate_auto_budget_is_half_of_physical() -> None:
    handler, _ = make_handler(
        runs=[],
        trees={},
        virtual_memory=SimpleNamespace(total=128 * GB, available=100 * GB),
    )
    assert handler._aggregate_limit_bytes() == 64 * GB

    config = TmuxConfig(agent_memory_total_limit_gb=48.0)
    handler_explicit, _ = make_handler(runs=[], trees={}, config=config)
    assert handler_explicit._aggregate_limit_bytes() == 48 * GB


@pytest.mark.asyncio
async def test_critical_pressure_kills_largest_agent_tree() -> None:
    runs = [
        make_run("run-1", terminal_id="gobby-a"),
        make_run("run-2", terminal_id="gobby-b"),
    ]
    handler, mocks = make_handler(
        runs=runs,
        trees={100: FakeProc(100, rss=2 * GB), 200: FakeProc(200, rss=3 * GB)},
        process_pids={"gobby-a": 100, "gobby-b": 200},
        virtual_memory=SimpleNamespace(total=128 * GB, available=int(2 * GB)),
    )
    assert await handler.check_agent_memory() == 1
    assert mocks["runtime"].killed_host_ids == ["gobby-b"]
    payload = mocks["cleanup_handler"].cleanup_agent.await_args.kwargs["terminal_payload"]
    assert "Critical system memory pressure" in payload


@pytest.mark.asyncio
async def test_pressure_warning_throttled(caplog: pytest.LogCaptureFixture) -> None:
    system_procs = [
        SimpleNamespace(
            info={
                "pid": 1000 + i,
                "name": f"proc-{i}",
                "memory_info": SimpleNamespace(rss=(12 - i) * GB),
            }
        )
        for i in range(12)
    ]
    handler, mocks = make_handler(
        runs=[make_run()],
        trees={100: FakeProc(100, rss=1 * GB)},
        # Between warn (8%) and critical (4%): warn only, no kill.
        virtual_memory=SimpleNamespace(total=128 * GB, available=int(128 * GB * 0.06)),
        process_iter=lambda attrs: system_procs,
        monotonic_values=[0.0, 100.0, 400.0],
    )
    with caplog.at_level(logging.WARNING):
        assert await handler.check_agent_memory() == 0
        first = sum("top consumers" in rec.message for rec in caplog.records)
        assert await handler.check_agent_memory() == 0
        second = sum("top consumers" in rec.message for rec in caplog.records)
        assert await handler.check_agent_memory() == 0
        third = sum("top consumers" in rec.message for rec in caplog.records)
    assert first == 1
    assert second == 1  # throttled at +100s
    assert third == 2  # fires again at +400s
    assert mocks["runtime"].killed_host_ids == []
    warn_text = next(rec.message for rec in caplog.records if "top consumers" in rec.message)
    assert "proc-0" in warn_text
    assert "proc-11" not in warn_text  # top-10 only


@pytest.mark.asyncio
async def test_failed_kill_skips_cleanup() -> None:
    handler, mocks = make_handler(
        runs=[make_run()],
        trees={100: FakeProc(100, rss=20 * GB)},
        kill_succeeds=False,
    )
    await handler.check_agent_memory()
    killed = await handler.check_agent_memory()
    assert killed == 0
    mocks["cleanup_handler"].cleanup_agent.assert_not_awaited()
    mocks["agent_run_manager"].clear_live_terminal.assert_not_called()


@pytest.mark.asyncio
async def test_one_process_scan_serves_every_run_in_a_tick() -> None:
    # Process.children(recursive=True) scanned every process on the machine
    # once per run, ~70 ms of GIL time each (#23359).
    grandchild = FakeProc(102, rss=3 * GB)
    trees = {
        100: FakeProc(100, rss=1 * GB, children=[FakeProc(101, rss=2 * GB, children=[grandchild])]),
        200: FakeProc(200, rss=4 * GB),
    }
    handler, mocks = make_handler(
        runs=[make_run("run-1", terminal_id="gobby-a"), make_run("run-2", terminal_id="gobby-b")],
        trees=trees,
        process_pids={"gobby-a": 100, "gobby-b": 200},
    )

    samples = await handler._collect_samples()

    assert {s.run.id: (s.total_rss, [p.pid for p in s.processes]) for s in samples} == {
        "run-1": (6 * GB, [100, 101, 102]),
        "run-2": (4 * GB, [200]),
    }
    assert mocks["ppid_scans"] == [4]


@pytest.mark.asyncio
async def test_a_descendant_older_than_its_root_holds_a_reused_pid_and_is_not_sampled() -> None:
    reused = FakeProc(101, rss=20 * GB, start_time=900.0, children=[FakeProc(102, rss=20 * GB)])
    handler, _ = make_handler(
        runs=[make_run()], trees={100: FakeProc(100, rss=1 * GB, children=[reused])}
    )

    samples = await handler._collect_samples()

    assert [(s.total_rss, [p.pid for p in s.processes]) for s in samples] == [(1 * GB, [100])]


@pytest.mark.asyncio
async def test_a_tick_without_a_live_native_root_scans_no_processes() -> None:
    handler, mocks = make_handler(
        runs=[make_run()], trees={100: FakeProc(100, rss=20 * GB, start_time=2000.0)}
    )

    assert await handler._collect_samples() == []
    assert mocks["ppid_scans"] == []
