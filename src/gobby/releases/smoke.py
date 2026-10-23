"""Fixed smokes a release batch runs after it activates the daemon."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from gobby.storage.agents import LocalAgentRunManager


@dataclass(frozen=True)
class SmokeResult:
    name: str
    ok: bool
    detail: str


class DaemonProbe(Protocol):
    """Daemon reads for the smokes. Status 0 means the daemon did not answer."""

    def get(self, path: str) -> tuple[int, object]: ...

    def call_tool(
        self, server: str, tool: str, arguments: dict[str, object]
    ) -> tuple[int, object]: ...


def active_runs(runs: LocalAgentRunManager, machine_id: str) -> frozenset[str]:
    """Return the machine's active runs that are not already reconciliation-fenced."""
    raise NotImplementedError


def run_smokes(
    probe: DaemonProbe,
    *,
    runs: LocalAgentRunManager,
    captured: frozenset[str],
    schema_pin: Callable[[], str | None],
    deadline: float,
    poll: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[SmokeResult]:
    """Run every fixed smoke and return one result per smoke."""
    raise NotImplementedError
