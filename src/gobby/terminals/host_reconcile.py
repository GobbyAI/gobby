"""Adoption reconciliation matrix for host list vs durable terminal rows."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from gobby.storage.terminals import Terminal, native_locator_key
from gobby.terminals.host_reap import recorded_process_group_is_alive
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)

KillFn = Callable[[str], Awaitable[None]]


class ReconcileError(RuntimeError):
    """Expected inventory inconsistency that may be reported without hiding other failures."""


class SupportsIdentityLookup(Protocol):
    def get(self, terminal_id: str) -> Terminal | None: ...

    def get_by_identity(self, terminal_id: str, spawn_key: str) -> Terminal | None: ...

    def list_live_by_machine(self, machine_id: str) -> list[Terminal]: ...

    def settle_lock(self, terminal_id: str) -> AbstractAsyncContextManager[None]: ...

    def promote_to_live(
        self,
        terminal_id: str,
        *,
        locator: Any,
        locator_key: str,
        host_epoch: str | None = None,
        session_name: str | None = None,
        window_id: str | None = None,
        title: str | None = None,
    ) -> Terminal | None: ...

    def fail_pending_attempt(
        self,
        terminal_id: str,
        *,
        attempt_generation: int,
        attempt_started_at: datetime,
    ) -> Terminal | None: ...

    def mark_exited(self, terminal_id: str) -> Terminal | None: ...

    def mark_orphaned(self, terminal_id: str) -> Terminal | None: ...

    def merge_process_reap_record(
        self,
        terminal_id: str,
        *,
        pgid: int,
        start_time: object,
    ) -> Terminal | None: ...


def _age_seconds(started: datetime) -> float:
    return max(0.0, (utc_now() - started).total_seconds())


def _interrupt_run(run_manager: Any, run_id: str | None) -> None:
    if not run_id or run_manager is None:
        return
    cancel = getattr(run_manager, "cancel", None)
    if callable(cancel):
        cancel(run_id, terminal_reason="daemon_stop")


def _durable_terminal_id(terminal_id: str) -> bool:
    """True when the host row names a gobby terminals-table id.

    Tmux observers use ``locator_key`` (``tmux:socket:pid:start:%pane``) as
    ``terminal_id``. Those slots are not unknown native children and must not
    be UUID-parsed or killed during adoption.
    """
    try:
        UUID(terminal_id)
    except ValueError:
        return False
    return True


async def reconcile_host_inventory(
    *,
    terminal_manager: SupportsIdentityLookup,
    machine_id: str,
    host_epoch: str,
    host_rows: Sequence[Any],
    spawn_in_doubt_seconds: float,
    run_manager: Any | None,
    kill: KillFn,
    unknown_grace_seconds: float = 0.0,
    settle_indeterminate: bool = False,
) -> str | None:
    """Apply the 3.1.9 adoption matrix. Returns last error or None."""
    list_reconcilable = getattr(terminal_manager, "list_reconcilable_by_machine", None)
    durable_rows = (
        list_reconcilable(machine_id)
        if callable(list_reconcilable)
        else terminal_manager.list_live_by_machine(machine_id)
    )
    db_rows = [row for row in durable_rows if row.backend == "native"]

    host_by_id = {(str(row.terminal_id), str(row.spawn_key)): row for row in host_rows}
    seen: set[str] = set()

    for row in host_rows:
        terminal_id = str(row.terminal_id)
        spawn_key = str(row.spawn_key)
        if not _durable_terminal_id(terminal_id):
            continue
        durable = terminal_manager.get_by_identity(terminal_id, spawn_key)
        commit_state = getattr(row, "commit_state", "committed")
        if durable is None:
            if unknown_grace_seconds > 0:
                import asyncio

                await asyncio.sleep(unknown_grace_seconds)
            again = terminal_manager.get_by_identity(terminal_id, spawn_key)
            if again is not None and again.state in {"pending", "live"}:
                continue
            await kill(str(getattr(row, "host_terminal_id", terminal_id)))
            continue
        seen.add(durable.id)
        if durable.state == "pending" and commit_state == "committed":
            host_terminal_id = str(getattr(row, "host_terminal_id", terminal_id))
            async with terminal_manager.settle_lock(durable.id):
                current = terminal_manager.get(durable.id)
                if current is not None and (
                    current.attempt_generation == durable.attempt_generation
                    and current.attempt_started_at == durable.attempt_started_at
                ):
                    terminal_manager.promote_to_live(
                        durable.id,
                        locator={"host_terminal_id": host_terminal_id},
                        locator_key=native_locator_key(host_epoch, host_terminal_id),
                        host_epoch=host_epoch,
                    )
        elif durable.state == "pending" and commit_state == "prepared":
            if settle_indeterminate:
                async with terminal_manager.settle_lock(durable.id):
                    current = terminal_manager.get(durable.id)
                    if current is None or (
                        current.attempt_generation != durable.attempt_generation
                        or current.attempt_started_at != durable.attempt_started_at
                    ):
                        continue
                    await kill(str(getattr(row, "host_terminal_id", terminal_id)))
                    terminal_manager.fail_pending_attempt(
                        durable.id,
                        attempt_generation=durable.attempt_generation,
                        attempt_started_at=durable.attempt_started_at,
                    )
                continue
            pgid = getattr(row, "pgid", None)
            start_time = getattr(row, "start_time", None)
            if isinstance(pgid, int):
                terminal_manager.merge_process_reap_record(
                    durable.id,
                    pgid=pgid,
                    start_time=start_time,
                )

    for durable in db_rows:
        if durable.id in seen:
            continue
        host_row = host_by_id.get((durable.id, str(durable.spawn_key)))
        if host_row is not None:
            continue
        if durable.state == "pending":
            if (
                not settle_indeterminate
                and _age_seconds(durable.attempt_started_at) < spawn_in_doubt_seconds
            ):
                continue
            async with terminal_manager.settle_lock(durable.id):
                current = terminal_manager.get(durable.id)
                if current is not None and (
                    current.attempt_generation == durable.attempt_generation
                    and current.attempt_started_at == durable.attempt_started_at
                ):
                    terminal_manager.fail_pending_attempt(
                        durable.id,
                        attempt_generation=durable.attempt_generation,
                        attempt_started_at=durable.attempt_started_at,
                    )
            continue
        if durable.state == "live" and durable.host_epoch == host_epoch:
            terminal_manager.mark_exited(durable.id)
            continue
        if durable.state == "live" and durable.host_epoch != host_epoch:
            terminal_manager.mark_orphaned(durable.id)
            _interrupt_run(run_manager, durable.agent_run_id)
            continue
        if (
            durable.state == "orphaned"
            and durable.host_epoch != host_epoch
            and not recorded_process_group_is_alive(durable.process)
        ):
            terminal_manager.mark_exited(durable.id)
    return None
