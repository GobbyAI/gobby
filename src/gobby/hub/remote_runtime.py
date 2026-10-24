"""The hub's runtime for a terminal hosted on another machine."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

from gobby.hub import remote_cancel
from gobby.hub.machine_routing import is_local_machine
from gobby.hub.remote_pane import RemotePane, remote_pane_store
from gobby.storage.agents import LocalAgentRunManager
from gobby.terminals.runtime import SnapshotResult, TerminalWriteError

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.terminals import AttachLocator, Terminal
    from gobby.terminals.runtime import (
        NamedKey,
        PreparedSpawn,
        SnapshotMode,
        TerminalHandle,
        TerminalRuntime,
        TerminalRuntimeRegistry,
        TerminalSpawnRequest,
        WriteOutcome,
    )

logger = logging.getLogger(__name__)


def _live_entry(terminal_id: str) -> RemotePane | None:
    entry = remote_pane_store().get(terminal_id)
    return None if entry is None or entry.stopped else entry


def _stored_tail(terminal_id: str) -> str:
    entry = _live_entry(terminal_id)
    if entry is None or entry.pane is None:
        return ""
    return entry.pane.tail


def remote_composer_state(terminal_id: str) -> str:
    """The composer the node reported for the terminal; a missing, stopped or pane-less entry is unknown."""
    entry = _live_entry(terminal_id)
    if entry is None or entry.pane is None:
        return "unknown"
    return entry.pane.composer.state


def resolve_terminal_runtime(
    registry: TerminalRuntimeRegistry, terminal: Terminal, db: HubDatabase
) -> TerminalRuntime:
    """The runtime that serves a terminal row; a row on another machine never reaches a local host."""
    if not is_local_machine(terminal.machine_id):
        return RemoteTerminalRuntime(db)
    return registry.resolve(terminal.backend)


class RemoteTerminalRuntime:
    """Reads and stops a node terminal from the hub's store, never a local host."""

    backend: Literal["tmux", "native"] = "native"

    def __init__(self, db: HubDatabase) -> None:
        self._db = db

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
        raise NotImplementedError("a node terminal spawns through its node, not the hub runtime")

    async def commit_spawn(self, prepared: PreparedSpawn) -> TerminalHandle:
        raise NotImplementedError("a node terminal spawns through its node, not the hub runtime")

    async def is_live(self, terminal: Terminal) -> bool:
        return _live_entry(terminal.id) is not None

    async def session_present(self, terminal: Terminal) -> bool:
        return _live_entry(terminal.id) is not None

    async def snapshot(
        self, terminal: Terminal, lines: int = 50, *, mode: SnapshotMode = "text"
    ) -> SnapshotResult:
        del mode
        rows = _stored_tail(terminal.id).splitlines()
        kept = rows[max(len(rows) - lines, 0) :]
        return SnapshotResult(
            text="\n".join(kept), truncated=False, dropped_bytes=None, total_bytes=None
        )

    async def snapshot_full(self, terminal: Terminal) -> SnapshotResult:
        return SnapshotResult(
            text=_stored_tail(terminal.id), truncated=False, dropped_bytes=None, total_bytes=None
        )

    async def write_text(self, terminal: Terminal, text: str, submit: bool) -> WriteOutcome:
        raise TerminalWriteError(stage="none")

    async def write_key(self, terminal: Terminal, key: NamedKey) -> WriteOutcome:
        raise TerminalWriteError(stage="none")

    async def write_input(self, terminal: Terminal, data: bytes) -> WriteOutcome:
        raise TerminalWriteError(stage="none")

    async def write_paste(self, terminal: Terminal, text: str) -> WriteOutcome:
        raise TerminalWriteError(stage="none")

    async def resize(self, terminal: Terminal, rows: int, cols: int) -> None:
        raise NotImplementedError(
            "a node terminal is resized through its node, not the hub runtime"
        )

    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        if terminal.agent_run_id is None:
            logger.warning("Node terminal %s has no agent run to stop", terminal.id)
            return
        run = await asyncio.to_thread(LocalAgentRunManager(self._db).get, terminal.agent_run_id)
        if run is None:
            logger.warning("Node terminal %s names a run that no longer exists", terminal.id)
            return
        result = await remote_cancel.cancel(run, self._db, signal_name="TERM", close_terminal=True)
        if result.get("error_code") == "not_stopped":
            await asyncio.sleep(grace_seconds)
            result = await remote_cancel.cancel(
                run, self._db, signal_name="KILL", close_terminal=True
            )
        if result.get("success") is True:
            remote_pane_store().mark_stopped(terminal.id)
            return
        logger.warning("Node did not stop run %s: %s", run.id, result.get("error"))

    async def attach_locator(self, terminal: Terminal) -> AttachLocator:
        raise NotImplementedError("a node terminal attaches through its node, not the hub runtime")
