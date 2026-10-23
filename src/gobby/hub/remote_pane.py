"""Hub-side state for node-reported panes: redaction, the revision store and attention."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

from gobby.agents.attention_tracker import AgentAttentionTracker, AttentionClassification
from gobby.hub.node_commands import PaneAttention, ReportAgent, ReportBody, ReportPane
from gobby.storage.agents import AgentRun
from gobby.storage.attention import AttentionKind
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.terminal_output import redact_terminal_output

_ATTENTION_KINDS: dict[str, AttentionKind] = {
    "actionable": "actionable",
    "non_actionable": "non_actionable",
}


@dataclass(frozen=True)
class RemotePane:
    machine_id: str
    run_id: str
    pane: ReportPane | None


@dataclass(frozen=True)
class _MachineState:
    revision: int
    panes: dict[str, RemotePane]


class RemotePaneStore:
    """The newest accepted pane snapshot for each node, keyed by terminal."""

    def __init__(self) -> None:
        self._machines: dict[str, _MachineState] = {}
        # Held across one report's store update and its attention writes, so a newer
        # report never interleaves with an older one still applying.
        self.apply_lock = asyncio.Lock()

    def apply_report(
        self, machine_id: str, revision: int, agents: list[ReportAgent]
    ) -> dict[str, RemotePane] | None:
        """Replace the machine's panes with a newer revision and return the panes it replaced.

        Returns None when the revision is not newer than the machine's last accepted one.
        """
        current = self._machines.get(machine_id)
        if current is not None and revision <= current.revision:
            return None
        self._machines[machine_id] = _MachineState(
            revision=revision,
            panes={
                agent.terminal_id: RemotePane(machine_id, agent.run_id, agent.pane)
                for agent in agents
            },
        )
        return current.panes if current is not None else {}

    def get(self, terminal_id: str) -> RemotePane | None:
        for state in self._machines.values():
            pane = state.panes.get(terminal_id)
            if pane is not None:
                return pane
        return None


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
    """Return the run a pane names, when its terminal belongs to the reporting machine."""
    if not (_is_uuid(run_id) and _is_uuid(terminal_id)):
        return None
    with db.transaction() as conn:
        row = conn.execute(
            """
            SELECT run.*, terminal.id AS terminal_id
            FROM agent_runs AS run
            JOIN terminals AS terminal ON terminal.agent_run_id = run.id
            WHERE run.id = %s AND terminal.id = %s AND terminal.machine_id = %s
            """,
            (run_id, terminal_id, machine_id),
        ).fetchone()
    return AgentRun.from_row(row) if row is not None else None


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


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
        run = await run_db(load_reported_run, db, machine_id, agent.run_id, agent.terminal_id)
        if run is None:
            continue
        prior = previous.get(agent.terminal_id)
        if prior is not None and prior.pane is not None and pane.injections > prior.pane.injections:
            await tracker.clear_after_injection(run)
        await tracker.apply(run, _attention_classification(pane.attention))


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
