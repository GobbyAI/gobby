"""Adoption reconciliation matrix for host list vs durable terminal rows."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from functools import partial
from typing import Any, Protocol
from uuid import UUID

from gobby.storage.terminals import Terminal, native_locator_key
from gobby.terminals.host_reap import recorded_process_group_is_alive
from gobby.terminals.in_doubt import in_doubt_spawns
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)

KillFn = Callable[[str], Awaitable[None]]


class ReconcileError(RuntimeError):
    """Expected inventory inconsistency that may be reported without hiding other failures."""


class SupportsIdentityLookup(Protocol):
    def get(self, terminal_id: str) -> Terminal | None: ...

    def get_many(self, terminal_ids: Sequence[str]) -> dict[str, Terminal]: ...

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

    def mark_exited_attempt(
        self,
        terminal_id: str,
        *,
        attempt_generation: int,
        attempt_started_at: datetime,
    ) -> Terminal | None: ...

    def mark_orphaned(self, terminal_id: str) -> Terminal | None: ...

    def record_orphan_identity(
        self,
        terminal_id: str,
        *,
        attempt_generation: int,
        attempt_started_at: datetime,
        locator: Any,
        locator_key: str,
        host_epoch: str,
        process: Any,
    ) -> Terminal | None: ...

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


def _orphan_and_interrupt(
    terminal_manager: SupportsIdentityLookup,
    run_manager: Any | None,
    terminal_id: str,
    run_id: str | None,
) -> None:
    terminal_manager.mark_orphaned(terminal_id)
    _interrupt_run(run_manager, run_id)


def _canonical_terminal_id(terminal_id: str) -> str | None:
    """Normalize a host row's gobby terminals-table id.

    Tmux observers use ``locator_key`` (``tmux:socket:pid:start:%pane``) as
    ``terminal_id``. Those slots are not unknown native children and must not
    be UUID-parsed or killed during adoption.
    """
    try:
        return str(UUID(terminal_id))
    except ValueError:
        return None


async def _settle_offloop(operation: Callable[[], Any]) -> Any:
    """Finish a hub mutation before releasing its caller's settlement lock."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


async def _record_listed_orphan(
    terminal_manager: SupportsIdentityLookup,
    durable: Terminal,
    row: Any,
    terminal_id: str,
    host_epoch: str,
) -> None:
    host_terminal_id = str(getattr(row, "host_terminal_id", terminal_id))
    process: dict[str, object] = {"host_terminal_id": host_terminal_id}
    pgid = getattr(row, "pgid", None)
    if isinstance(pgid, int):
        process["pgid"] = pgid
        start_time = getattr(row, "start_time", None)
        if start_time is not None:
            process["start_time"] = start_time
    async with terminal_manager.settle_lock(durable.id):
        current = await asyncio.to_thread(terminal_manager.get, durable.id)
        if (
            current is None
            or current.state != "orphaned"
            or current.spawn_key != durable.spawn_key
            or current.attempt_generation != durable.attempt_generation
            or current.attempt_started_at != durable.attempt_started_at
            or in_doubt_spawns.holds(durable.id)
        ):
            return
        await _settle_offloop(
            partial(
                terminal_manager.record_orphan_identity,
                durable.id,
                attempt_generation=current.attempt_generation,
                attempt_started_at=current.attempt_started_at,
                locator={"host_terminal_id": host_terminal_id},
                locator_key=native_locator_key(host_epoch, host_terminal_id),
                host_epoch=host_epoch,
                process=process,
            )
        )


async def _settle_absent_orphan(
    terminal_manager: SupportsIdentityLookup,
    durable: Terminal,
    host_epoch: str,
) -> None:
    """Exit an orphan whose current host answered its listing without it.

    Reconcile runs only on a listing the host returned, so absence here is
    proven; an unreachable host never reaches this path.
    """
    async with terminal_manager.settle_lock(durable.id):
        current = await asyncio.to_thread(terminal_manager.get, durable.id)
        if (
            current is None
            or current.state != "orphaned"
            or current.host_epoch != host_epoch
            or current.attempt_generation != durable.attempt_generation
            or current.attempt_started_at != durable.attempt_started_at
            or in_doubt_spawns.holds(durable.id)
        ):
            return
        await _settle_offloop(
            partial(
                terminal_manager.mark_exited_attempt,
                durable.id,
                attempt_generation=current.attempt_generation,
                attempt_started_at=current.attempt_started_at,
            )
        )


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
    durable_rows = await asyncio.to_thread(
        list_reconcilable if callable(list_reconcilable) else terminal_manager.list_live_by_machine,
        machine_id,
    )
    db_rows = [row for row in durable_rows if row.backend == "native"]

    host_by_id = {
        (_canonical_terminal_id(str(row.terminal_id)), str(row.spawn_key)): row for row in host_rows
    }
    host_ids = [
        terminal_id
        for row in host_rows
        if (terminal_id := _canonical_terminal_id(str(row.terminal_id))) is not None
    ]
    by_id = await asyncio.to_thread(terminal_manager.get_many, host_ids)
    unknown_ids = [
        terminal_id
        for row in host_rows
        if (terminal_id := _canonical_terminal_id(str(row.terminal_id))) is not None
        and (terminal_id not in by_id or by_id[terminal_id].spawn_key != str(row.spawn_key))
    ]
    if unknown_ids:
        if unknown_grace_seconds > 0:
            await asyncio.sleep(unknown_grace_seconds)
        retry_by_id = await asyncio.to_thread(terminal_manager.get_many, unknown_ids)
    else:
        retry_by_id = {}
    seen: set[str] = set()

    for row in host_rows:
        terminal_id = _canonical_terminal_id(str(row.terminal_id))
        spawn_key = str(row.spawn_key)
        # A held id's local prepare can still create or bind after this
        # snapshot; its owner, not reconcile, decides the outcome.
        if terminal_id is None or in_doubt_spawns.holds(terminal_id):
            continue
        durable = by_id.get(terminal_id)
        if durable is not None and durable.spawn_key != spawn_key:
            durable = None
        commit_state = getattr(row, "commit_state", "committed")
        if durable is None:
            again = retry_by_id.get(terminal_id)
            if again is not None and again.spawn_key != spawn_key:
                again = None
            if again is not None and again.state in {"pending", "live"}:
                continue
            await kill(str(getattr(row, "host_terminal_id", terminal_id)))
            continue
        seen.add(durable.id)
        if durable.state == "pending" and commit_state == "committed":
            host_terminal_id = str(getattr(row, "host_terminal_id", terminal_id))
            async with terminal_manager.settle_lock(durable.id):
                current = await asyncio.to_thread(terminal_manager.get, durable.id)
                if current is not None and (
                    current.attempt_generation == durable.attempt_generation
                    and current.attempt_started_at == durable.attempt_started_at
                ):
                    await _settle_offloop(
                        partial(
                            terminal_manager.promote_to_live,
                            durable.id,
                            locator={"host_terminal_id": host_terminal_id},
                            locator_key=native_locator_key(host_epoch, host_terminal_id),
                            host_epoch=host_epoch,
                        )
                    )
        elif durable.state == "pending" and commit_state == "prepared":
            if settle_indeterminate:
                async with terminal_manager.settle_lock(durable.id):
                    current = await asyncio.to_thread(terminal_manager.get, durable.id)
                    if current is None or (
                        current.attempt_generation != durable.attempt_generation
                        or current.attempt_started_at != durable.attempt_started_at
                    ):
                        continue
                    await kill(str(getattr(row, "host_terminal_id", terminal_id)))
                    await _settle_offloop(
                        partial(
                            terminal_manager.fail_pending_attempt,
                            durable.id,
                            attempt_generation=durable.attempt_generation,
                            attempt_started_at=durable.attempt_started_at,
                        )
                    )
                continue
            pgid = getattr(row, "pgid", None)
            start_time = getattr(row, "start_time", None)
            if isinstance(pgid, int):
                await _settle_offloop(
                    partial(
                        terminal_manager.merge_process_reap_record,
                        durable.id,
                        pgid=pgid,
                        start_time=start_time,
                    )
                )
        elif durable.state == "orphaned":
            # The host still lists this orphan: record its current identity so
            # terminal_kill can address it. Reconcile never kills it here.
            await _record_listed_orphan(terminal_manager, durable, row, terminal_id, host_epoch)

    for durable in db_rows:
        if durable.id in seen or in_doubt_spawns.holds(durable.id):
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
                current = await asyncio.to_thread(terminal_manager.get, durable.id)
                if current is not None and (
                    current.attempt_generation == durable.attempt_generation
                    and current.attempt_started_at == durable.attempt_started_at
                ):
                    await _settle_offloop(
                        partial(
                            terminal_manager.fail_pending_attempt,
                            durable.id,
                            attempt_generation=durable.attempt_generation,
                            attempt_started_at=durable.attempt_started_at,
                        )
                    )
            continue
        if durable.state == "live" and durable.host_epoch == host_epoch:
            # Absence settles only a dead group: a kill in flight holds the
            # lock, and a host may drop a slot before its group is gone.
            async with terminal_manager.settle_lock(durable.id):
                current = await asyncio.to_thread(terminal_manager.get, durable.id)
                if (
                    current is not None
                    and current.state == "live"
                    and current.host_epoch == host_epoch
                    and current.attempt_generation == durable.attempt_generation
                    and current.attempt_started_at == durable.attempt_started_at
                    and not recorded_process_group_is_alive(current.process)
                ):
                    await _settle_offloop(partial(terminal_manager.mark_exited, durable.id))
            continue
        if durable.state == "live" and durable.host_epoch != host_epoch:
            await _settle_offloop(
                partial(
                    _orphan_and_interrupt,
                    terminal_manager,
                    run_manager,
                    durable.id,
                    durable.agent_run_id,
                )
            )
            continue
        if durable.state == "orphaned" and durable.host_epoch == host_epoch:
            await _settle_absent_orphan(terminal_manager, durable, host_epoch)
            continue
        if (
            durable.state == "orphaned"
            and durable.host_epoch != host_epoch
            and not recorded_process_group_is_alive(durable.process)
        ):
            await _settle_offloop(partial(terminal_manager.mark_exited, durable.id))
    return None
