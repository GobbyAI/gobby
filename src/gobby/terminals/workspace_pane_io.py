"""Pane terminal I/O ops that ``WorkspaceOps`` inherits: send, read, and wait."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.detection.safe_regex import InvalidPatternError, RegexOutcome, compile_safe_regex
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import Terminal, TerminalManager
from gobby.storage.workspaces import WorkspacePane
from gobby.terminals.key_bytes import normalize_named_key
from gobby.terminals.runtime import (
    InputPayloadTooLargeError,
    SnapshotResult,
    TerminalRuntime,
    TerminalRuntimeRegistry,
    TerminalWriteError,
    UnregisteredBackendError,
)
from gobby.terminals.workspace_contract import PaneOutputWait, WorkspaceOpError
from gobby.terminals.workspace_writes import (
    PaneWrite,
    WorkspacePaneWriteError,
    write_workspace_pane,
)
from gobby.terminals.write_coordinator import IdempotencyConflictError, WriteCoordinator

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

WAIT_CAPTURE_LINES = 200
WAIT_CAPTURE_FAILURE_LIMIT = 3
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_ACTIVE_STATES = frozenset({"pending", "live"})


class WorkspacePaneIOMixin:
    """Write to, read, and wait on the terminal behind a workspace pane."""

    _terminals: TerminalManager
    _registry: TerminalRuntimeRegistry
    _coordinator: WriteCoordinator
    _sessions: SessionManager
    _detection_registry: DetectionManifestRegistry
    if TYPE_CHECKING:

        async def _pane_terminal(
            self, actor: str, pane: str, node: str | None
        ) -> tuple[WorkspacePane, Terminal]: ...

        async def _db(self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T: ...

    async def pane_send_text(
        self,
        actor: str,
        pane: str,
        text: str,
        *,
        submit: bool = False,
        idempotency_key: str | None = None,
        node: str | None = None,
    ) -> PaneWrite:
        return await self._write(
            actor,
            pane,
            node,
            kind="text",
            payload=text,
            submit=submit,
            key=idempotency_key,
            verify_submit=submit,
        )

    async def pane_send_keys(
        self,
        actor: str,
        pane: str,
        keys: str,
        *,
        literal: bool = True,
        idempotency_key: str | None = None,
        node: str | None = None,
    ) -> PaneWrite:
        """Write ``keys`` as ``send_keys`` does: a trailing newline submits, names are keys."""
        if literal and keys.endswith("\n"):
            return await self._write(
                actor,
                pane,
                node,
                kind="text",
                payload=keys.rstrip("\n"),
                submit=True,
                key=idempotency_key,
            )
        named = None if literal else normalize_named_key(keys)
        if not literal and named is None:
            raise WorkspaceOpError(
                "invalid_op", f"Unsupported named key for managed terminal: {keys}"
            )
        return await self._write(
            actor,
            pane,
            node,
            kind="key" if named else "text",
            payload=named or keys,
            submit=False,
            key=idempotency_key,
        )

    async def pane_read(
        self, actor: str, pane: str, *, lines: int = 50, node: str | None = None
    ) -> SnapshotResult:
        if lines < 1:
            raise WorkspaceOpError("invalid_op", f"Pane read lines must be positive, not {lines}")
        _row, terminal = await self._pane_terminal(actor, pane, node)
        runtime = self._runtime(terminal)
        try:
            return await runtime.snapshot(terminal, lines)
        except Exception as exc:
            raise WorkspaceOpError("terminal_failed", f"Pane read failed: {exc}") from exc

    async def pane_wait_for_output(
        self,
        actor: str,
        pane: str,
        pattern: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 2.0,
        node: str | None = None,
    ) -> PaneOutputWait:
        """Poll the pane's terminal until ``pattern`` matches, it ends, or time runs out."""
        if not (math.isfinite(timeout_seconds) and math.isfinite(poll_interval_seconds)):
            raise WorkspaceOpError("invalid_op", "Wait durations must be finite numbers")
        # The websocket op has no MCP clamp. 300s matches wait_for_pane_output.
        timeout_seconds = min(timeout_seconds, 300.0)
        try:
            matcher = compile_safe_regex(pattern)
        except InvalidPatternError as exc:
            raise WorkspaceOpError("invalid_op", str(exc)) from exc
        _row, terminal = await self._pane_terminal(actor, pane, node)
        runtime = self._runtime(terminal)
        interval = max(0.1, min(poll_interval_seconds, 30.0))
        deadline = time.monotonic() + timeout_seconds
        failures = 0
        while True:
            snapshot: SnapshotResult | None = None
            try:
                snapshot = await runtime.snapshot(terminal, WAIT_CAPTURE_LINES)
            except Exception:
                logger.warning("Failed to capture terminal %s output", terminal.id, exc_info=True)
            if snapshot is not None:
                failures = 0
                match = matcher.search(snapshot.text)
                if match.outcome is RegexOutcome.PATTERN_TIMEOUT:
                    raise WorkspaceOpError(
                        "invalid_op", "pattern execution exceeded its time budget"
                    )
                if match.matched:
                    return PaneOutputWait(True, "matched", snapshot)
            current = await self._db(self._terminals.get, terminal.id)
            if current is None or current.state not in _ACTIVE_STATES:
                return PaneOutputWait(False, "pane_lost", snapshot)
            if snapshot is None:
                failures += 1
                if failures >= WAIT_CAPTURE_FAILURE_LIMIT:
                    raise WorkspaceOpError(
                        "terminal_failed", "terminal capture failed three consecutive times"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return PaneOutputWait(False, "timeout", snapshot)
            await asyncio.sleep(min(interval, remaining))

    def _runtime(self, terminal: Terminal) -> TerminalRuntime:
        try:
            return self._registry.resolve(terminal.backend)
        except UnregisteredBackendError as exc:
            raise WorkspaceOpError(
                "terminal_failed", f"No runtime serves {terminal.backend} terminals"
            ) from exc

    async def _write(
        self,
        actor: str,
        pane: str,
        node: str | None,
        *,
        kind: Literal["text", "key"],
        payload: str,
        submit: bool,
        key: str | None,
        verify_submit: bool = False,
    ) -> PaneWrite:
        resolved_key = key or secrets.token_hex(16)
        if IDEMPOTENCY_KEY_PATTERN.fullmatch(resolved_key) is None:
            raise WorkspaceOpError(
                "invalid_op", "idempotency_key must be 1 to 128 characters from [A-Za-z0-9._:-]"
            )
        row, terminal = await self._pane_terminal(actor, pane, node)
        try:
            return await write_workspace_pane(
                self._coordinator,
                self._sessions,
                self._detection_registry,
                self._runtime(terminal),
                terminal,
                pane_id=row.id,
                kind=kind,
                payload=payload,
                submit=submit,
                verify_submit=verify_submit,
                idempotency_key=resolved_key,
            )
        except (IdempotencyConflictError, InputPayloadTooLargeError) as exc:
            raise WorkspaceOpError("invalid_op", str(exc)) from exc
        except TerminalWriteError as exc:
            raise WorkspaceOpError("terminal_failed", str(exc)) from exc
        except WorkspacePaneWriteError as exc:
            raise WorkspaceOpError("terminal_failed", str(exc)) from exc
