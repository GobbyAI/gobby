"""The composer gate for watchdog writes into a managed agent terminal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gobby.terminals.composer_ledger import read_composer

if TYPE_CHECKING:
    from gobby.storage.agents import AgentRun
    from gobby.terminals.services import TerminalServices


def composer_refuses_automation(services: TerminalServices | None, run: AgentRun) -> bool:
    """True when the ledger reads the run's composer as an operator draft or unknown.

    An empty composer or daemon-held text takes the write. Unknown covers a provider
    limit block, an interrupt, a host gap and an untracked terminal. A run with no
    live terminal is left to the write itself to fail.
    """
    terminal = None if services is None else services.terminal_for(run)
    return terminal is not None and read_composer(terminal.id).state not in {"empty", "held"}
