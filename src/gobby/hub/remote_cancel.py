"""Cancel an agent run on the node that owns it, through the node command channel."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import ValidationError

from gobby.hub.node_client import NodeCommandError, node_command_client
from gobby.hub.node_commands import StoppedResult
from gobby.storage.agents import AgentRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import TerminalManager
from gobby.terminals.termination import settle_proven_exit


async def cancel(
    run: AgentRun,
    db: HubDatabase,
    *,
    signal_name: str,
    close_terminal: bool,
) -> dict[str, Any]:
    """Send agent.cancel to the run's node. A stopped run's terminal row settles when asked."""
    if run.terminal_id is None:
        return {"success": False, "error": "agent run has no terminal", "error_code": "no_terminal"}
    payload = {
        "run_id": run.id,
        "terminal_id": run.terminal_id,
        "signal": signal_name,
        "close_terminal": close_terminal,
    }
    try:
        reply = await node_command_client().send(run.machine_id, "agent.cancel", payload)
    except NodeCommandError as exc:
        # not_found: the node runs nothing for this run and recorded the cancel, so it is stopped.
        if exc.code != "not_found":
            return {"success": False, "error": str(exc), "error_code": exc.code}
    else:
        try:
            stopped = StoppedResult.model_validate(reply).stopped
        except ValidationError:
            return {
                "success": False,
                "error": f"Node machine {run.machine_id} sent an unreadable cancel reply",
                "error_code": "node_reply_lost",
            }
        if not stopped:
            return {
                "success": False,
                "error": f"Node machine {run.machine_id} did not stop run {run.id}",
                "error_code": "not_stopped",
            }
    if close_terminal:
        await _settle_terminal(db, run.terminal_id)
    return {
        "success": True,
        "method": "node_command",
        "message": f"Stopped run {run.id} on machine {run.machine_id}",
    }


async def _settle_terminal(db: HubDatabase, terminal_id: str) -> None:
    # The row's compare-and-set transitions make a concurrent settle safe without a shared lock.
    terminals = TerminalManager(db)
    current = await asyncio.to_thread(terminals.get, terminal_id)
    if current is not None and current.state != "exited":
        await settle_proven_exit(terminals, current)
