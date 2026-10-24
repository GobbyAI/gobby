"""Settle remote runs and terminals from the node's reports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from gobby.agents.recovery_state import is_recovery_protected
from gobby.hub import node_reports
from gobby.hub.node_client import NodeCommandError, node_command_client
from gobby.hub.node_commands import ReportAgent, ReportBody, ReportExit, StoppedResult
from gobby.storage.agents import TERMINAL_AGENT_RUN_STATUSES
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.agents import AgentRun
    from gobby.storage.terminals import Terminal

_ACTIVE_TERMINAL_STATES = ("pending", "live", "orphaned")
_RUN_PAGE = 100
_Key = tuple[str | None, str]


class RemoteRunReconciler:
    """Settles the runs and terminals of each node that reports to the hub."""

    def __init__(
        self,
        *,
        run_manager: Any,
        terminals: Any,
        cleanup_agent: Any,
        dead_reason: Any,
        run_db: Any,
        in_doubt_seconds: float,
    ) -> None:
        self._run_manager = run_manager
        self._terminals = terminals
        self._cleanup_agent = cleanup_agent
        self._dead_reason = dead_reason
        self._run_db = run_db
        self._in_doubt_seconds = in_doubt_seconds
        # Per machine: terminal rows absent from the last report, and runs swept as orphaned.
        self._absent: dict[str, set[_Key]] = {}
        self._swept: dict[str, set[str]] = {}

    async def reconcile(self) -> int:
        local = require_machine_id()
        cleaned = 0
        for machine_id, taken in node_reports.node_report_inbox().take().items():
            if machine_id == local:
                continue
            cleaned += await self._reconcile_machine(machine_id, taken.snapshot, taken.exits)
        return cleaned

    async def _reconcile_machine(
        self, machine_id: str, report: ReportBody, exits: tuple[ReportExit, ...]
    ) -> int:
        cleaned = 0
        for exit_report in exits:
            cleaned += await self._settle_exit(machine_id, exit_report)

        rows = await self._run_db(self._terminals.list_reconcilable_by_machine, machine_id)
        rows = [row for row in rows if row.agent_run_id is not None]
        rows = await self._unprotected(rows)
        host_epoch = str(report.host_epoch)
        accepted = set(report.accepted)
        by_key: dict[_Key, Terminal] = {(row.agent_run_id, row.id): row for row in rows}

        confirmed: set[_Key] = set()
        for agent in report.agents:
            row = by_key.get((agent.run_id, agent.terminal_id))
            if row is None:
                continue
            if row.state == "pending":
                await self._run_db(self._record_spawn_identity, row, agent, host_epoch)
                confirmed.add((agent.run_id, agent.terminal_id))
            elif row.host_epoch == host_epoch:
                confirmed.add((agent.run_id, agent.terminal_id))

        absent_before = self._absent.get(machine_id, set())
        absent_now: set[_Key] = set()
        for row in rows:
            key: _Key = (row.agent_run_id, row.id)
            if key in confirmed or row.agent_run_id in accepted:
                continue
            absent_now.add(key)
            if key in absent_before:
                cleaned += await self._settle_absent(machine_id, row)
        self._absent[machine_id] = absent_now

        cleaned += await self._sweep_runs(machine_id)
        return cleaned

    async def _unprotected(self, rows: list[Terminal]) -> list[Terminal]:
        kept: list[Terminal] = []
        for row in rows:
            run = await self._run_db(self._run_manager.get, row.agent_run_id)
            if run is None or not is_recovery_protected(run):
                kept.append(row)
        return kept

    async def _settle_exit(self, machine_id: str, report: ReportExit) -> int:
        # A launched exit waits for a second report like an absent row; dead_reason decides.
        if report.launched:
            return 0
        row = await self._run_db(self._terminals.get, report.terminal_id)
        if (
            row is None
            or row.machine_id != machine_id
            or row.agent_run_id != report.run_id
            or row.state == "exited"
        ):
            return 0
        run = await self._run_db(self._run_manager.get, report.run_id)
        if run is not None and is_recovery_protected(run):
            return 0
        settle = self._terminals.fail_pending if row.state == "pending" else None
        await self._run_db(settle or self._terminals.mark_exited, row.id)
        if run is None:
            return 0
        return await self._cleanup_if_active(
            run, f"remote spawn failed on {machine_id}: {report.reason}"
        )

    def _record_spawn_identity(self, row: Terminal, agent: ReportAgent, host_epoch: str) -> None:
        self._terminals.promote_to_live(
            row.id,
            locator={"host_terminal_id": agent.locator},
            locator_key=agent.locator_key,
            host_epoch=host_epoch,
        )
        run = self._run_manager.get(agent.run_id)
        if run is None or run.status in TERMINAL_AGENT_RUN_STATUSES:
            self._terminals.mark_exited(row.id)
            return
        self._run_manager.update_runtime(run.id, pid=agent.pid, terminal_id=row.id)

    async def _settle_absent(self, machine_id: str, row: Terminal) -> int:
        if row.agent_run_id is None:
            return 0
        if row.state == "pending":
            if not self._in_doubt(row):
                return 0
            return await self._fence_spawn(machine_id, row)
        await self._run_db(self._terminals.mark_exited, row.id)
        return await self._settle_dead(row.agent_run_id)

    async def _fence_spawn(self, machine_id: str, row: Terminal) -> int:
        if not await self._spawn_stopped(machine_id, row):
            return 0
        await self._run_db(self._terminals.fail_pending, row.id)
        return await self._settle_dead(row.agent_run_id)

    async def _settle_dead(self, run_id: str | None) -> int:
        if run_id is None:
            return 0
        run = await self._run_db(self._run_manager.get, run_id)
        if run is None or run.status in TERMINAL_AGENT_RUN_STATUSES:
            return 0
        reason = await self._dead_reason(run)
        if reason is None:
            return 0
        await self._cleanup_agent(run, terminal_payload=reason)
        return 1

    async def _spawn_stopped(self, machine_id: str, row: Terminal) -> bool:
        payload = {
            "run_id": row.agent_run_id,
            "terminal_id": row.id,
            "signal": "KILL",
            "close_terminal": True,
        }
        try:
            reply = await node_command_client().send(machine_id, "agent.cancel", payload)
        except NodeCommandError as exc:
            # not_found: the node runs nothing for this run, so the spawn is stopped.
            return exc.code == "not_found"
        try:
            return bool(StoppedResult.model_validate(reply).stopped)
        except ValidationError:
            return False

    async def _sweep_runs(self, machine_id: str) -> int:
        runs = await self._active_runs(machine_id)
        swept_before = self._swept.get(machine_id, set())
        swept_now: set[str] = set()
        cleaned = 0
        for run in runs:
            if is_recovery_protected(run) or not run.child_session_id:
                continue
            terminals = await self._run_db(self._terminals.list_for_session, run.child_session_id)
            if not terminals or any(t.state in _ACTIVE_TERMINAL_STATES for t in terminals):
                continue
            swept_now.add(run.id)
            if run.id not in swept_before:
                continue
            fresh = await self._run_db(self._run_manager.get, run.id)
            if fresh is None or fresh.status in TERMINAL_AGENT_RUN_STATUSES:
                continue
            await self._cleanup_agent(
                fresh, terminal_payload="remote terminal exited before its run settled"
            )
            cleaned += 1
        self._swept[machine_id] = swept_now
        return cleaned

    async def _active_runs(self, machine_id: str) -> list[AgentRun]:
        runs: list[AgentRun] = []
        offset = 0
        while True:
            page = await self._run_db(
                self._run_manager.list_active_for_machine,
                machine_id,
                limit=_RUN_PAGE,
                offset=offset,
            )
            runs.extend(page)
            if len(page) < _RUN_PAGE:
                return runs
            offset += _RUN_PAGE

    async def _cleanup_if_active(self, run: AgentRun, payload: str) -> int:
        if run.status in TERMINAL_AGENT_RUN_STATUSES:
            return 0
        await self._cleanup_agent(run, terminal_payload=payload)
        return 1

    def _in_doubt(self, row: Terminal) -> bool:
        deadline = timedelta(seconds=self._in_doubt_seconds)
        return row.attempt_started_at < datetime.now(UTC) - deadline
