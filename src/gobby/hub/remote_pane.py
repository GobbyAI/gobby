"""Hub-side state for node-reported panes: redaction, the revision store and attention."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from gobby.agents.attention_tracker import AgentAttentionTracker
from gobby.hub.node_commands import ReportAgent, ReportBody, ReportPane
from gobby.storage.agents import AgentRun
from gobby.storage.hub.protocol import HubDatabase


@dataclass
class RemotePane:
    machine_id: str
    run_id: str
    pane: ReportPane | None
    stopped: bool = False


class RemotePaneStore:
    def apply_report(
        self, machine_id: str, revision: int, agents: list[ReportAgent]
    ) -> dict[str, RemotePane] | None:
        raise NotImplementedError

    def mark_stopped(self, terminal_id: str) -> None:
        raise NotImplementedError

    def get(self, terminal_id: str) -> RemotePane | None:
        raise NotImplementedError


@lru_cache(maxsize=1)
def remote_pane_store() -> RemotePaneStore:
    return RemotePaneStore()


def redact_report_panes(report: ReportBody) -> ReportBody:
    raise NotImplementedError


def load_reported_run(
    db: HubDatabase, machine_id: str, run_id: str, terminal_id: str
) -> AgentRun | None:
    raise NotImplementedError


async def apply_pane_state(
    db: HubDatabase,
    tracker: AgentAttentionTracker,
    *,
    machine_id: str,
    previous: dict[str, RemotePane],
    agents: list[ReportAgent],
    run_db: Callable[..., Awaitable[Any]],
) -> None:
    raise NotImplementedError
