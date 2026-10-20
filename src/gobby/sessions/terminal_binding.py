"""Repair native-pane attachment from an already recorded, live CLI identity."""

from __future__ import annotations

import math
import os
import subprocess
from dataclasses import dataclass, replace
from typing import Any, Protocol

import psutil

from gobby import terminal_ownership
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.terminals import Terminal, TerminalManager
from gobby.terminals.foreground import shell_pid
from gobby.utils.machine_id import get_machine_id


class SessionBindingStore(Protocol):
    db: HubDatabase

    def backfill_terminal_context(
        self,
        session_id: str,
        terminal_context: dict[str, Any] | None,
        current: Session | None = None,
    ) -> tuple[Session | None, bool]: ...


@dataclass(frozen=True)
class TerminalBindingResult:
    status: str
    reason: str
    terminal_id: str | None = None


def _native_seat_context(session: Session, terminal: Terminal) -> dict[str, Any] | None:
    """Verify the CLI, shell generation, foreground group and native environment."""
    inspection = terminal_ownership.inspect_foreground_ownership(
        session,
        process_factory=psutil.Process,
        process_group_factory=os.getpgid,
        foreground_group_factory=terminal_ownership.foreground_process_group,
    )
    if inspection.state != terminal_ownership.OwnershipState.OWNED:
        return None
    context = session.terminal_context or {}
    pid = context.get("parent_pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    process = psutil.Process(pid)
    if process.name() != session.source or "app-server" in process.cmdline():
        return None
    environment = process.environ()
    if environment.get("TMUX_PANE") or environment.get("GOBBY_TERMINAL_ID") != terminal.id:
        return None
    if context.get("gobby_host_epoch") not in (None, terminal.host_epoch):
        return None
    native_pid = shell_pid(terminal)
    ancestors = {ancestor.pid for ancestor in process.parents()}
    if native_pid is None or native_pid not in ancestors:
        return None
    shell = psutil.Process(native_pid)
    native_process = terminal.process or {}
    start = native_process.get("start_time")
    if isinstance(start, bool) or not isinstance(start, (int, float)):
        return None
    if abs(shell.create_time() - start) >= 1.0:
        return None
    tty = process.terminal()
    if not tty or tty != shell.terminal():
        return None
    return {
        "parent_pid": pid,
        "parent_create_time": process.create_time(),
        "parent_name": process.name(),
        "tty": tty,
        "gobby_terminal_id": terminal.id,
        "gobby_pane_ref": environment.get("GOBBY_PANE_REF"),
        "gobby_host_epoch": terminal.host_epoch,
    }


def repair_native_terminal_binding(
    manager: SessionBindingStore,
    session: Session,
    *,
    selected_terminal_id: str | None = None,
) -> TerminalBindingResult:
    """Bind only a uniquely OS-verified native pane; never infer a shared host's seat."""
    refused = TerminalBindingResult(
        "unbound",
        "No unique verified native seat; operator terminal registration is required.",
    )
    if (
        session.session_type != "terminal"
        or session.machine_id != get_machine_id()
        or session.status not in terminal_ownership.TERMINAL_OWNER_STATUSES
    ):
        return refused
    context = session.terminal_context or {}
    if context.get("tmux_pane"):
        return TerminalBindingResult("unbound", "Native repair does not attach tmux panes.")
    terminals = TerminalManager(manager.db)
    try:
        verified_session = session
        if selected_terminal_id is not None:
            # This selector is authorized by the operator-authenticated adapter.
            # The daemon, never the caller, supplies the actual process identity.
            selected = terminals.get(selected_terminal_id)
            if (
                selected is None
                or selected.state != "live"
                or selected.backend != "native"
                or selected.machine_id != session.machine_id
                or selected.project_id != session.project_id
                or selected.agent_run_id is not None
            ):
                return refused
            native_pid = shell_pid(selected)
            if native_pid is None:
                return refused
            foreground_pid = terminal_ownership.foreground_process_group(native_pid)
            if foreground_pid <= 0:
                return refused
            foreground = psutil.Process(foreground_pid)
            verified_session = replace(
                session,
                terminal_context={
                    "parent_pid": foreground_pid,
                    "parent_create_time": foreground.create_time(),
                },
            )
        candidates: list[tuple[Terminal, dict[str, Any]]] = []
        for row in terminals.list_live_by_machine(session.machine_id):
            if (
                row.backend != "native"
                or row.project_id != session.project_id
                or row.agent_run_id is not None
                or not row.host_epoch
                or (selected_terminal_id is not None and row.id != selected_terminal_id)
            ):
                continue
            verified = _native_seat_context(verified_session, row)
            if verified is not None:
                candidates.append((row, verified))
        if len(candidates) != 1:
            return refused
        terminal, verified = candidates[0]
        with manager.db.transaction() as conn:
            persisted = conn.execute(
                "SELECT * FROM sessions WHERE id = %s FOR UPDATE", (session.id,)
            ).fetchone()
            if persisted is None:
                return refused
            locked_session = Session.from_row(persisted)
            if (
                locked_session.status not in terminal_ownership.TERMINAL_OWNER_STATUSES
                or locked_session.machine_id != session.machine_id
                or locked_session.project_id != session.project_id
                or locked_session.source != session.source
                or locked_session.external_id != session.external_id
                or (selected_terminal_id is None and locked_session.terminal_context != context)
            ):
                return refused
            conn.execute("SELECT id FROM terminals WHERE id = %s FOR UPDATE", (terminal.id,))
            competitors = conn.execute(
                """
                SELECT terminal_context->>'parent_create_time' AS create_time
                FROM sessions WHERE machine_id = %s AND id <> %s
                  AND status = ANY(%s) AND session_type = 'terminal'
                  AND terminal_context->>'parent_pid' = %s
                """,
                (
                    session.machine_id,
                    session.id,
                    list(terminal_ownership.TERMINAL_OWNER_STATUSES),
                    str(verified["parent_pid"]),
                ),
            ).fetchall()
            for competitor in competitors:
                try:
                    other_start = float(competitor["create_time"])
                except (TypeError, ValueError):
                    return TerminalBindingResult(
                        "unbound", "Competing CLI identity is unverifiable."
                    )
                if (
                    not math.isfinite(other_start)
                    or abs(other_start - verified["parent_create_time"]) < 1.0
                ):
                    return TerminalBindingResult(
                        "unbound", "Multiple live sessions claim the CLI process."
                    )
            current_terminal = terminals.get(terminal.id)
            if (
                current_terminal is None
                or current_terminal.state != "live"
                or current_terminal.machine_id != session.machine_id
                or current_terminal.project_id != session.project_id
                or current_terminal.host_epoch != terminal.host_epoch
                or _native_seat_context(verified_session, current_terminal) != verified
            ):
                return refused
            if terminals.bind_session(terminal.id, session.id, session.project_id) is None:
                return TerminalBindingResult("unbound", "Native pane has another live owner.")
            updated, _ = manager.backfill_terminal_context(session.id, verified, locked_session)
            if updated is None:
                raise ValueError("Session disappeared during terminal registration")
        return TerminalBindingResult(
            "bound", "Verified foreground CLI and native shell generation.", terminal.id
        )
    except (psutil.Error, OSError, ValueError, subprocess.SubprocessError):
        return refused
