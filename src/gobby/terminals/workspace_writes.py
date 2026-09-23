"""Verified terminal writes for workspace panes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from gobby.agents.detection.provider import DetectionRegistry
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import Terminal
from gobby.terminals.pane_io import composer_reader, submit_coordinated_text
from gobby.terminals.runtime import Delivered, IndeterminateWrite, TerminalRuntime
from gobby.terminals.write_coordinator import WriteCoordinator, WriteRequest


@dataclass(frozen=True, slots=True)
class PaneWrite:
    """A pane write the coordinator accepted; indeterminate when it may have landed."""

    idempotency_key: str
    indeterminate: bool
    detail: str | None = None


class WorkspacePaneWriteError(RuntimeError):
    """A coordinator result that cannot be represented as a pane write."""


async def write_workspace_pane(
    coordinator: WriteCoordinator,
    sessions: SessionManager,
    detection_registry: DetectionRegistry,
    runtime: TerminalRuntime,
    terminal: Terminal,
    *,
    pane_id: str,
    kind: Literal["text", "key"],
    payload: str,
    submit: bool,
    verify_submit: bool,
    idempotency_key: str,
) -> PaneWrite:
    """Write once, or verify a requested text submission against its composer."""
    action_key = f"workspace-pane-send:{pane_id}:{idempotency_key}"
    if kind == "text" and verify_submit:
        session = None if terminal.session_id is None else sessions.get(terminal.session_id)
        cli_source = None if session is None else session.source
        result = await submit_coordinated_text(
            coordinator,
            runtime,
            terminal,
            payload,
            terminal.session_id or pane_id,
            action_key=action_key,
            idempotency_key=idempotency_key,
            label="pane text",
            cli_source=cli_source,
            composer_read=composer_reader(detection_registry, cli_source),
        )
        if result.ok:
            return PaneWrite(idempotency_key, indeterminate=False)
        detail = result.reason or "submission could not be verified"
        return PaneWrite(
            idempotency_key,
            indeterminate=True,
            detail=f"Pane text was not submitted: {detail}",
        )

    outcome = await coordinator.write(
        WriteRequest(
            terminal_id=terminal.id,
            action_key=action_key,
            origin="daemon",
            kind=kind,
            payload=payload,
            submit=submit,
            idempotency_key=idempotency_key,
        )
    )
    if isinstance(outcome, IndeterminateWrite):
        return PaneWrite(idempotency_key, indeterminate=True, detail=str(outcome) or None)
    if not isinstance(outcome, Delivered):
        raise WorkspacePaneWriteError(f"Pane write was not delivered ({type(outcome).__name__})")
    return PaneWrite(idempotency_key, indeterminate=False)
