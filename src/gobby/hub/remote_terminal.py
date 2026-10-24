"""Remote terminal writes, routed through the node that owns the terminal."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from gobby.hub.node_client import NodeCommandError, node_command_client
from gobby.terminals.runtime import Delivered, IndeterminateWrite, TerminalWriteError, WriteOutcome

if TYPE_CHECKING:
    from gobby.storage.terminals import Terminal
    from gobby.terminals.write_coordinator import WriteRequest


class NodeTerminalWriteError(TerminalWriteError):
    """A node refused a remote write; ``code`` names the refusal."""

    def __init__(self, code: str, *, stage: Literal["none", "partial"]) -> None:
        super().__init__(stage=stage)
        self.code = code


async def write(terminal: Terminal, request: WriteRequest) -> WriteOutcome:
    """Send one write to the node that owns ``terminal`` and map its delivery stage."""
    payload = {
        "terminal_id": terminal.id,
        "kind": request.kind,
        "data": request.payload,
        "submit": request.submit,
    }
    try:
        await node_command_client().send(terminal.machine_id, "terminal.write", payload)
    except NodeCommandError as exc:
        if exc.stage is None:
            # The command may have reached the node, so the outcome is unknown.
            return IndeterminateWrite(detail=exc.code)
        raise NodeTerminalWriteError(exc.code, stage=exc.stage) from exc
    return Delivered()
