"""Hub-side state for node-reported panes: redaction, the revision store and attention."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

from gobby.agents.attention_tracker import AgentAttentionTracker, AttentionClassification
from gobby.hub.node_commands import PaneAttention, ReportAgent, ReportBody, ReportPane
from gobby.hub.node_reports import is_uuid
from gobby.storage.agents import AgentRun
from gobby.storage.attention import AttentionKind
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.terminal_output import redact_terminal_output

logger = logging.getLogger(__name__)

_ATTENTION_KINDS: dict[str, AttentionKind] = {
    "actionable": "actionable",
    "non_actionable": "non_actionable",
}


@dataclass(frozen=True)
class RemotePane:
    machine_id: str
    run_id: str
    pane: ReportPane | None
    stopped: bool = False


@dataclass(frozen=True)
class _MachineState:
    revision: int
    panes: dict[str, RemotePane]


class RemotePaneStore:
    """The newest accepted pane snapshot for each node, keyed by terminal."""

    def __init__(self) -> None:
        self._machines: dict[str, _MachineState] = {}
        self._lock = threading.Lock()
        # Held across one report's store update and its attention writes, so a newer
        # report never interleaves with an older one still applying.
        self.apply_lock = asyncio.Lock()

    def apply_report(
        self, machine_id: str, revision: int, agents: list[ReportAgent]
    ) -> dict[str, RemotePane] | None:
        """Replace the machine's panes with a newer revision and return the panes it replaced.

        Returns None when the revision is not newer than the machine's last accepted one.
        """
        with self._lock:
            current = self._machines.get(machine_id)
            if current is not None and revision <= current.revision:
                return None
            previous = current.panes if current is not None else {}
            self._machines[machine_id] = _MachineState(
                revision=revision,
                panes={
                    agent.terminal_id: _entry(machine_id, agent, previous.get(agent.terminal_id))
                    for agent in agents
                },
            )
            return previous

    def mark_stopped(self, terminal_id: str, machine_id: str) -> None:
        """Flag the terminal's entry as stopped on the machine that hosts it."""
        with self._lock:
            state = self._machines.get(machine_id)
            if state is None:
                return
            pane = state.panes.get(terminal_id)
            if pane is not None:
                state.panes[terminal_id] = replace(pane, stopped=True)

    def get(self, terminal_id: str, machine_id: str) -> RemotePane | None:
        """The machine's own entry for the terminal; another machine's never answers."""
        with self._lock:
            state = self._machines.get(machine_id)
            return None if state is None else state.panes.get(terminal_id)


def _entry(machine_id: str, agent: ReportAgent, prior: RemotePane | None) -> RemotePane:
    # A stop carries over a newer report only while the same run still holds the terminal.
    stopped = prior is not None and prior.stopped and prior.run_id == agent.run_id
    return RemotePane(machine_id, agent.run_id, agent.pane, stopped=stopped)


@lru_cache(maxsize=1)
def remote_pane_store() -> RemotePaneStore:
    return RemotePaneStore()


def redact_report_panes(report: ReportBody) -> ReportBody:
    """Return the report with every string in each agent's pane redacted; fingerprints pass."""
    agents = [
        agent
        if agent.pane is None
        else agent.model_copy(update={"pane": _redact_model(agent.pane)})
        for agent in report.agents
    ]
    return report.model_copy(update={"agents": agents})


def _redact_model[M: BaseModel](model: M) -> M:
    return model.model_copy(update={name: _redact(name, value) for name, value in model})


def _redact(name: str, value: object) -> object:
    if name == "fingerprint":
        return value
    if isinstance(value, str):
        return redact_terminal_output(value)
    if isinstance(value, BaseModel):
        return _redact_model(value)
    if isinstance(value, list):
        return [_redact(name, item) for item in value]
    return value


def load_reported_run(
    db: HubDatabase, machine_id: str, run_id: str, terminal_id: str
) -> AgentRun | None:
    """Return the run a pane names, when its terminal belongs to the reporting machine.

    The run and its terminal must both still be active, as the local attention scan
    requires, so a stopped run's late report applies nothing.
    """
    if not (is_uuid(run_id) and is_uuid(terminal_id)):
        return None
    with db.transaction() as conn:
        row = conn.execute(
            """
            SELECT run.*, terminal.id AS terminal_id
            FROM agent_runs AS run
            JOIN terminals AS terminal ON terminal.agent_run_id = run.id
            WHERE run.id = %s AND terminal.id = %s AND terminal.machine_id = %s
              AND run.status IN ('queued', 'running', 'pending')
              AND terminal.state IN ('pending', 'live')
            """,
            (run_id, terminal_id, machine_id),
        ).fetchone()
    return AgentRun.from_row(row) if row is not None else None


async def apply_pane_state(
    db: HubDatabase,
    tracker: AgentAttentionTracker,
    *,
    machine_id: str,
    previous: dict[str, RemotePane],
    agents: list[ReportAgent],
    run_db: Callable[..., Awaitable[Any]],
) -> None:
    """Apply each reported pane's injections and attention to the run it names."""
    for agent in agents:
        pane = agent.pane
        if pane is None:
            continue
        # A terminal stopped on the hub is no longer attended, whatever the node reports.
        current = remote_pane_store().get(agent.terminal_id, machine_id)
        if current is not None and current.stopped:
            continue
        try:
            run = await run_db(load_reported_run, db, machine_id, agent.run_id, agent.terminal_id)
            if run is None:
                continue
            prior = previous.get(agent.terminal_id)
            if (
                prior is not None
                and prior.pane is not None
                and pane.injections > prior.pane.injections
            ):
                await tracker.clear_after_injection(run)
            await tracker.apply(run, _attention_classification(pane.attention))
        except Exception as exc:
            # One pane that fails must not hold back the panes reported after it.
            logger.warning("Node pane state not applied for one pane: %s", type(exc).__name__)


async def apply_report_panes(
    db: HubDatabase,
    tracker: AgentAttentionTracker,
    *,
    machine_id: str,
    revision: int,
    agents: list[ReportAgent],
    run_db: Callable[..., Awaitable[Any]],
) -> None:
    """Store a node report's panes, then apply their attention in revision order."""
    store = remote_pane_store()
    async with store.apply_lock:
        previous = store.apply_report(machine_id, revision, agents)
        if previous is None:
            return
        await apply_pane_state(
            db, tracker, machine_id=machine_id, previous=previous, agents=agents, run_db=run_db
        )


def _attention_classification(attention: PaneAttention | None) -> AttentionClassification | None:
    if attention is None:
        return None
    kind = _ATTENTION_KINDS.get(attention.kind)
    if kind is None:
        raise ValueError(f"unknown attention kind {attention.kind!r}")
    return AttentionClassification(
        reason=attention.reason,
        kind=kind,
        fingerprint=attention.fingerprint,
        payload=attention.payload.model_dump(),
        dismissed=attention.dismissed,
    )
