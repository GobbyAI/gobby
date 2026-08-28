"""Composition-root terminal services shared by monitors, MCP, and hooks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from gobby.storage.terminals import Terminal
from gobby.terminals.lookup import active_terminal_for_run
from gobby.terminals.runtime import SnapshotResult, TerminalRuntime, WriteOutcome
from gobby.terminals.write_coordinator import WriteCoordinator, WriteRequest


@dataclass
class TerminalServices:
    """One manager, registry, and coordinator shared by in-process consumers."""

    manager: Any
    registry: Any
    coordinator: WriteCoordinator | None = None

    def terminal_for(self, run: Any) -> Terminal | None:
        return active_terminal_for_run(self.manager, run)

    def runtime_for(self, terminal: Terminal) -> TerminalRuntime:
        return cast("TerminalRuntime", self.registry.resolve(terminal.backend))

    async def snapshot(self, run: Any, lines: int = 50) -> SnapshotResult | None:
        terminal = self.terminal_for(run)
        if terminal is None:
            return None
        return await self.runtime_for(terminal).snapshot(terminal, lines)

    async def snapshot_full(self, run: Any) -> SnapshotResult | None:
        terminal = self.terminal_for(run)
        if terminal is None:
            return None
        return await self.runtime_for(terminal).snapshot_full(terminal)

    async def is_live(self, run: Any) -> bool:
        terminal = self.terminal_for(run)
        if terminal is None:
            return False
        return await self.runtime_for(terminal).is_live(terminal)

    async def terminate(self, run: Any, grace_seconds: float = 5.0) -> bool:
        terminal = self.terminal_for(run)
        if terminal is None:
            return False
        await self.runtime_for(terminal).terminate(terminal, grace_seconds)
        return True

    async def write(
        self,
        run: Any,
        *,
        action_key: str,
        origin: Literal["operator", "automatic", "attention"] = "automatic",
        kind: Literal["text", "key", "paste"] = "text",
        payload: str,
        submit: bool = False,
    ) -> WriteOutcome | None:
        if self.coordinator is None:
            return None
        terminal = self.terminal_for(run)
        if terminal is None:
            return None
        return await self.coordinator.write(
            WriteRequest(
                terminal_id=terminal.id,
                action_key=action_key,
                origin=origin,
                kind=kind,
                payload=payload,
                submit=submit,
            )
        )

    async def run_sequence(
        self,
        run: Any,
        *,
        action_key: str,
        origin: Literal["operator", "automatic", "attention"] = "automatic",
        steps: list[WriteRequest],
    ) -> WriteOutcome | None:
        if self.coordinator is None:
            return None
        terminal = self.terminal_for(run)
        if terminal is None:
            return None
        return await self.coordinator.run_sequence(
            terminal.id,
            action_key=action_key,
            origin=origin,
            steps=steps,
        )
