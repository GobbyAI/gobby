"""Repair native-pane attachment from an already recorded, live CLI identity."""

from __future__ import annotations

import math
import os
import subprocess
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast

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


def _native_seat_context(
    session: Session, terminal: Terminal, process: psutil.Process
) -> dict[str, Any] | None:
    """Verify the CLI, shell generation, foreground group and native environment."""
    context = session.terminal_context or {}
    pid = process.pid
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
        selected: Terminal | None = None
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
        # Inspect the CLI once, rather than once per terminal in the scan.
        inspection = terminal_ownership.inspect_foreground_ownership(
            verified_session,
            process_factory=psutil.Process,
            process_group_factory=os.getpgid,
            foreground_group_factory=terminal_ownership.foreground_process_group,
        )
        if inspection.state != terminal_ownership.OwnershipState.OWNED:
            return refused
        # OWNED carries the process created by our psutil factory, with normalized PID.
        process = cast(psutil.Process, inspection.process)
        observed_name = process.name()
        if observed_name != session.source or "app-server" in process.cmdline():
            return TerminalBindingResult(
                "unbound", f"Expected foreground {session.source} CLI; observed {observed_name!r}."
            )

        def eligible(row: Terminal) -> bool:
            return (
                row.state == "live"
                and row.backend == "native"
                and row.machine_id == session.machine_id
                and row.project_id == session.project_id
                and row.agent_run_id is None
                and bool(row.host_epoch)
            )

        bound_id = context.get("gobby_terminal_id")
        if selected_terminal_id is None and isinstance(bound_id, str):
            bound = terminals.get(bound_id)
            if (
                bound is not None
                and eligible(bound)
                and bound.session_id == session.id
                and context.get("gobby_host_epoch") == bound.host_epoch
                and _native_seat_context(session, bound, process) is not None
            ):
                # Ordinary hooks verify the saved seat without locks, scans or writes.
                return TerminalBindingResult("bound", "Existing native binding verified.", bound.id)
        candidates: list[tuple[Terminal, dict[str, Any]]] = []
        rows = (
            [selected]
            if selected is not None
            else terminals.list_live_by_machine(session.machine_id)
        )
        for row in rows:
            if not eligible(row):
                continue
            verified = _native_seat_context(verified_session, row, process)
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
            if selected_terminal_id is not None:
                holder = conn.execute(
                    "SELECT s.status FROM sessions s JOIN terminals t ON t.session_id=s.id "
                    "WHERE t.id=%s AND s.id<>%s FOR UPDATE OF s",
                    (terminal.id, session.id),
                ).fetchone()
                if holder is not None and holder["status"] != "expired":
                    return TerminalBindingResult(
                        "unbound", "Operator attachment refuses another non-expired holder."
                    )
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
            current_inspection = terminal_ownership.inspect_foreground_ownership(
                verified_session,
                process_factory=psutil.Process,
                process_group_factory=os.getpgid,
                foreground_group_factory=terminal_ownership.foreground_process_group,
            )
            if (
                current_terminal is None
                or current_terminal.state != "live"
                or current_terminal.machine_id != session.machine_id
                or current_terminal.project_id != session.project_id
                or current_terminal.host_epoch != terminal.host_epoch
                or current_inspection.state != terminal_ownership.OwnershipState.OWNED
                or _native_seat_context(verified_session, current_terminal, process) != verified
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
