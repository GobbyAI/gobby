"""Memory watchdog for agent process trees and system memory pressure.

Enforcement is polling-based (psutil RSS of tmux pane process trees) because
RLIMIT_AS/RLIMIT_DATA are not enforced for Mach VM allocations on Darwin.
Incident #18196: two agent-coalition python processes reached 175GB/169GB RSS
and OOM-crashed the host with no guard anywhere in the stack.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import psutil

from gobby.agents.capture import TerminationErrorCode
from gobby.agents.kill import kill_agent
from gobby.utils.datetime import parse_stored_datetime
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.agents.agent_cleanup import AgentCleanupHandler
    from gobby.agents.tmux.session_manager import TmuxSessionManager
    from gobby.config.tmux import TmuxConfig
    from gobby.storage.agents import AgentRun, LocalAgentRunManager, TerminalAction
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

_BYTES_PER_GB = 1024**3
_BREAKDOWN_TOP_N = 15
_PRESSURE_WARN_INTERVAL_SECONDS = 300.0


@dataclass
class _ProcessSample:
    pid: int
    name: str
    cmdline: str
    rss: int


@dataclass
class _TreeSample:
    run: AgentRun
    total_rss: int
    processes: list[_ProcessSample]
    kill_eligible: bool


def _format_breakdown(processes: list[_ProcessSample], total_rss: int, limit_bytes: int) -> str:
    lines = [
        f"tree total {total_rss / _BYTES_PER_GB:.2f}GB, limit {limit_bytes / _BYTES_PER_GB:.2f}GB"
    ]
    ranked = sorted(processes, key=lambda p: p.rss, reverse=True)[:_BREAKDOWN_TOP_N]
    for proc in ranked:
        lines.append(
            f"  pid={proc.pid} rss={proc.rss / (1024**2):.0f}MB name={proc.name} cmd={proc.cmdline}"
        )
    return "\n".join(lines)


class MemoryWatchdogHandler:
    """Kills agent tmux sessions whose process trees exceed memory caps.

    Three layers, checked every lifecycle tick:
    1. Per-agent cap (``agent_memory_limit_gb``) after N consecutive breaches.
    2. Aggregate cap across all agent trees (``agent_memory_total_limit_gb``,
       0 = 50% of physical RAM): kills the largest tree until under budget.
    3. System pressure: warn (with system-wide top consumers) below the warn
       threshold; below the critical threshold kill the largest agent tree —
       non-agent processes are never killed, only reported.
    """

    def __init__(
        self,
        *,
        agent_run_manager: LocalAgentRunManager,
        db: HubDatabase,
        tmux: TmuxSessionManager,
        cleanup_handler: AgentCleanupHandler,
        tmux_config: TmuxConfig,
        run_db: Callable[..., Awaitable[Any]] | None = None,
        kill_agent_fn: Callable[[AgentRun], Awaitable[dict[str, Any]]] | None = None,
        process_factory: Callable[[int], Any] = psutil.Process,
        virtual_memory_fn: Callable[[], Any] = psutil.virtual_memory,
        process_iter_fn: Callable[..., Any] = psutil.process_iter,
        monotonic: Callable[[], float] = time.monotonic,
        terminal_services: Any | None = None,
    ) -> None:
        self._agent_run_manager = agent_run_manager
        self._db = db
        self._tmux = tmux
        self._terminal_services = terminal_services
        self._cleanup_handler = cleanup_handler
        self._config = tmux_config
        self._run_db_callback = run_db
        self._kill_agent_fn = kill_agent_fn
        self._process_factory = process_factory
        self._virtual_memory = virtual_memory_fn
        self._process_iter = process_iter_fn
        self._monotonic = monotonic
        self._breach_counts: dict[str, int] = {}
        self._aggregate_breach_count = 0
        self._last_pressure_warning = float("-inf")

    async def _run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db_callback is not None:
            return await self._run_db_callback(func, *args, **kwargs)
        return func(*args, **kwargs)

    def _aggregate_limit_bytes(self) -> int:
        configured = self._config.agent_memory_total_limit_gb
        if configured > 0:
            return int(configured * _BYTES_PER_GB)
        try:
            total = int(self._virtual_memory().total)
        except Exception:  # pragma: no cover - psutil failure is non-fatal
            return 0
        return total // 2

    def _measure_tree(self, pane_pid: int) -> tuple[int, list[_ProcessSample]]:
        try:
            root = self._process_factory(pane_pid)
            candidates = [root, *root.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return 0, []
        samples: list[_ProcessSample] = []
        for proc in candidates:
            try:
                samples.append(
                    _ProcessSample(
                        pid=proc.pid,
                        name=proc.name(),
                        cmdline=" ".join(proc.cmdline())[:200],
                        rss=int(proc.memory_info().rss),
                    )
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return sum(sample.rss for sample in samples), samples

    def _in_grace_period(self, run: AgentRun) -> bool:
        if not run.started_at:
            return False
        started = parse_stored_datetime(run.started_at)
        if started is None:
            return False
        age = (datetime.now(UTC) - started).total_seconds()
        return age < self._config.memory_watchdog_grace_seconds

    async def _kill_run(self, sample: _TreeSample, reason: str) -> bool:
        run = sample.run
        breakdown = _format_breakdown(
            sample.processes,
            sample.total_rss,
            int(self._config.agent_memory_limit_gb * _BYTES_PER_GB),
        )
        message = f"{reason}\n{breakdown}"
        if self._config.memory_watchdog_action == "warn":
            logger.warning("Memory watchdog (warn-only) would kill agent %s: %s", run.id, message)
            return False

        logger.warning("Memory watchdog killing agent %s: %s", run.id, message)
        services = self._terminal_services
        terminal = None if services is None else services.terminal_for(run)
        if services is None or terminal is None:
            result = await kill_agent(
                run,
                self._db,
                signal_name="TERM",
                timeout=5.0,
                close_terminal=False,
            )
            if not result.get("success"):
                logger.warning(
                    "Memory watchdog kill failed for run %s: %s",
                    run.id,
                    result.get("error") or result.get("message"),
                )
                return False
            await self._cleanup_handler.cleanup_agent(run, terminal_payload=message)
            self._breach_counts.pop(run.id, None)
            return True

        async def terminalize(
            _action: TerminalAction,
            payload: str | None,
        ) -> AgentRun | None:
            await self._cleanup_handler.cleanup_agent(
                run,
                terminal_payload=payload or message,
            )
            return cast(
                "AgentRun | None",
                await self._run_db(self._agent_run_manager.get, run.id),
            )

        from gobby.agents.capture import terminate_managed_runtime_async

        termination = await terminate_managed_runtime_async(
            storage=self._agent_run_manager,
            run=run,
            terminal=terminal,
            runtime=services.runtime_for(terminal),
            action="fail",
            reason=message,
            terminalize=terminalize,
        )
        if not termination.success:
            if termination.error_code == TerminationErrorCode.ALREADY_TERMINAL:
                # Another path terminalized the run first; nothing left to kill.
                logger.info(
                    "Memory watchdog kill skipped for run %s: %s",
                    run.id,
                    termination.error,
                )
                return False
            logger.warning(
                "Memory watchdog kill failed for run %s: %s (%s)",
                run.id,
                termination.error,
                termination.error_code,
            )
            return False
        self._breach_counts.pop(run.id, None)
        return True

    async def _collect_samples(self) -> list[_TreeSample]:
        runs = await self._run_db(
            self._agent_run_manager.list_active_for_machine,
            require_machine_id(),
        )
        samples: list[_TreeSample] = []
        for run in runs:
            terminal = (
                None
                if self._terminal_services is None
                else self._terminal_services.terminal_for(run)
            )
            if terminal is None or not terminal.session_name:
                continue
            try:
                pane_pid = await self._tmux.get_pane_pid(terminal.session_name)
            except Exception:
                pane_pid = None
            if pane_pid is None:
                self._breach_counts.pop(run.id, None)
                continue
            total_rss, processes = self._measure_tree(pane_pid)
            if not processes:
                self._breach_counts.pop(run.id, None)
                continue
            samples.append(
                _TreeSample(
                    run=run,
                    total_rss=total_rss,
                    processes=processes,
                    kill_eligible=not self._in_grace_period(run),
                )
            )
        return samples

    async def _enforce_per_agent(self, samples: list[_TreeSample]) -> int:
        limit_bytes = int(self._config.agent_memory_limit_gb * _BYTES_PER_GB)
        killed = 0
        for sample in samples:
            run_id = sample.run.id
            if sample.total_rss <= limit_bytes or not sample.kill_eligible:
                self._breach_counts.pop(run_id, None)
                continue
            count = self._breach_counts.get(run_id, 0) + 1
            self._breach_counts[run_id] = count
            if count < self._config.memory_watchdog_consecutive_breaches:
                logger.warning(
                    "Agent %s over memory limit (%.2fGB > %.2fGB), breach %d/%d",
                    run_id,
                    sample.total_rss / _BYTES_PER_GB,
                    self._config.agent_memory_limit_gb,
                    count,
                    self._config.memory_watchdog_consecutive_breaches,
                )
                continue
            if await self._kill_run(
                sample,
                f"Agent process tree exceeded memory limit "
                f"({sample.total_rss / _BYTES_PER_GB:.2f}GB > "
                f"{self._config.agent_memory_limit_gb:.2f}GB)",
            ):
                killed += 1
                sample.kill_eligible = False
        return killed

    async def _enforce_aggregate(self, samples: list[_TreeSample]) -> int:
        limit_bytes = self._aggregate_limit_bytes()
        if limit_bytes <= 0:
            return 0
        total = sum(sample.total_rss for sample in samples)
        if total <= limit_bytes:
            self._aggregate_breach_count = 0
            return 0
        self._aggregate_breach_count += 1
        if self._aggregate_breach_count < self._config.memory_watchdog_consecutive_breaches:
            logger.warning(
                "Aggregate agent memory over budget (%.2fGB > %.2fGB), breach %d/%d",
                total / _BYTES_PER_GB,
                limit_bytes / _BYTES_PER_GB,
                self._aggregate_breach_count,
                self._config.memory_watchdog_consecutive_breaches,
            )
            return 0
        # Kill the largest eligible tree; remaining overage is handled next tick.
        eligible = [s for s in samples if s.kill_eligible]
        if not eligible:
            logger.warning(
                "Aggregate agent memory over budget (%.2fGB > %.2fGB) but no "
                "kill-eligible agent trees",
                total / _BYTES_PER_GB,
                limit_bytes / _BYTES_PER_GB,
            )
            return 0
        largest = max(eligible, key=lambda s: s.total_rss)
        killed = await self._kill_run(
            largest,
            f"Aggregate agent memory exceeded budget "
            f"({total / _BYTES_PER_GB:.2f}GB > {limit_bytes / _BYTES_PER_GB:.2f}GB); "
            f"killing largest tree",
        )
        if killed:
            self._aggregate_breach_count = 0
            largest.kill_eligible = False
            return 1
        return 0

    def _log_system_top_consumers(self) -> None:
        now = self._monotonic()
        if now - self._last_pressure_warning < _PRESSURE_WARN_INTERVAL_SECONDS:
            return
        self._last_pressure_warning = now
        consumers: list[tuple[int, str, int]] = []
        try:
            for proc in self._process_iter(["pid", "name", "memory_info"]):
                info = proc.info
                mem = info.get("memory_info")
                if mem is None:
                    continue
                consumers.append((info["pid"], info.get("name") or "?", int(mem.rss)))
        except Exception:  # pragma: no cover - psutil failure is non-fatal
            return
        consumers.sort(key=lambda item: item[2], reverse=True)
        lines = [
            f"  pid={pid} rss={rss / _BYTES_PER_GB:.2f}GB name={name}"
            for pid, name, rss in consumers[:10]
        ]
        logger.warning("System memory pressure — top consumers:\n%s", "\n".join(lines))

    async def _check_system_pressure(self, samples: list[_TreeSample]) -> int:
        try:
            vm = self._virtual_memory()
            available_percent = vm.available / vm.total * 100.0
        except Exception:  # pragma: no cover - psutil failure is non-fatal
            return 0
        if available_percent >= self._config.system_memory_warn_available_percent:
            return 0
        self._log_system_top_consumers()
        if available_percent >= self._config.system_memory_critical_available_percent:
            return 0
        eligible = [s for s in samples if s.kill_eligible]
        if not eligible:
            logger.warning(
                "Critical system memory pressure (%.1f%% available) with no "
                "kill-eligible agent trees; non-agent processes are report-only",
                available_percent,
            )
            return 0
        largest = max(eligible, key=lambda s: s.total_rss)
        killed = await self._kill_run(
            largest,
            f"Critical system memory pressure ({available_percent:.1f}% available < "
            f"{self._config.system_memory_critical_available_percent:.1f}%); "
            f"killing largest agent tree",
        )
        return 1 if killed else 0

    async def check_agent_memory(self) -> int:
        """Run all memory checks; returns the number of agents killed."""
        if not self._config.memory_watchdog_enabled:
            return 0
        samples = await self._collect_samples()
        active_ids = {sample.run.id for sample in samples}
        for run_id in list(self._breach_counts):
            if run_id not in active_ids:
                self._breach_counts.pop(run_id, None)
        killed = await self._enforce_per_agent(samples)
        killed += await self._enforce_aggregate(samples)
        killed += await self._check_system_pressure(samples)
        return killed
