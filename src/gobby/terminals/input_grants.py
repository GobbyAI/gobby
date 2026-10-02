"""Make the gterm frame-stream input grant follow the daemon's writer lease.

gclient types straight into the host once the daemon has granted its attachment
(memory b59e4ce9). The daemon still decides who may write: every lease transition
reconciles the host grant with the holder through :func:`sync_host_input_grant`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

from gobby.terminals.host_client import HostCommandError, HostEpochChangedError
from gobby.terminals.runtime import TerminalWriteError

if TYPE_CHECKING:
    from gobby.storage.terminals import Terminal, TerminalManager

logger = logging.getLogger(__name__)
NATIVE_INPUT_REBIND_SECONDS = 30.0
HandoffKey = tuple[str, str, str, str]


class LeaseHolder(Protocol):
    """The lease attachment that just became the holder (read-only view)."""

    @property
    def attachment_id(self) -> str: ...

    @property
    def frame_delivery(self) -> str: ...


class InputGrantRuntime(Protocol):
    async def grant_input(self, terminal: Terminal, attachment_id: str) -> None: ...

    async def revoke_input(self, terminal: Terminal, attachment_id: str | None = None) -> None: ...


# Refusals and outages the host or its transport can answer a grant with. Anything
# else is a bug and propagates.
_HOST_FAILURES = (
    HostCommandError,
    HostEpochChangedError,
    TerminalWriteError,
    ConnectionError,
    OSError,
)


async def sync_host_input_grant(
    runtime: InputGrantRuntime,
    terminal: Terminal | None,
    holder: LeaseHolder | None,
    *,
    manager: TerminalManager | None = None,
    reason: str = "lease_change",
) -> bool | None:
    """Reconcile the host grant with the lease holder after one transition.

    Returns ``None`` when no grant applies: the terminal is not native, or the
    holder is not a direct viewer (a web or proxied holder, or no holder), in which
    case any standing grant is revoked first. Returns ``True`` when the host
    accepted the grant and ``False`` when it refused or could not be reached.
    """
    if terminal is None or terminal.backend != "native":
        return None
    if reason == "daemon_shutdown":
        if manager is None or holder is None or holder.frame_delivery != "direct":
            return False
        return await asyncio.to_thread(
            manager.record_native_input_handoff, terminal, holder.attachment_id
        )
    if holder is None or holder.frame_delivery != "direct":
        try:
            await runtime.revoke_input(terminal)
        except _HOST_FAILURES as exc:
            level = (
                logging.DEBUG
                if isinstance(exc, HostCommandError) and exc.code == "not_found"
                else logging.WARNING
            )
            logger.log(level, "revoke_input for terminal %s failed: %s", terminal.id, exc)
        else:
            if manager is not None:
                await asyncio.to_thread(manager.clear_native_input_handoff, terminal.id)
        return None
    try:
        await runtime.grant_input(terminal, holder.attachment_id)
    except _HOST_FAILURES as exc:
        logger.warning(
            "grant_input for terminal %s attachment %s failed: %s",
            terminal.id,
            holder.attachment_id,
            exc,
        )
        return False
    if manager is not None:
        await asyncio.to_thread(manager.clear_native_input_handoff, terminal.id)
    return True


async def expire_native_input_handoffs(
    manager: TerminalManager,
    client: Any,
    machine_id: str,
    host_epoch: str,
    deadlines: dict[HandoffKey, float],
    now: float,
) -> None:
    """Retire unrebound authority through the host's attachment-guarded revoke."""
    terminals = await asyncio.to_thread(manager.list_native_input_handoffs, machine_id)
    present: set[HandoffKey] = set()
    for terminal in terminals:
        record = (terminal.process or {}).get("native_input_handoff")
        if not isinstance(record, dict):
            continue
        attachment_id = record.get("attachment_id")
        epoch = record.get("host_epoch")
        host_terminal_id = record.get("host_terminal_id")
        if not (
            isinstance(attachment_id, str)
            and attachment_id
            and isinstance(epoch, str)
            and isinstance(host_terminal_id, str)
            and host_terminal_id
        ):
            continue
        key = (terminal.id, attachment_id, epoch, host_terminal_id)
        present.add(key)
        if (
            epoch != host_epoch
            or terminal.host_epoch != epoch
            or (terminal.locator or {}).get("host_terminal_id") != host_terminal_id
        ):
            # The old host identity cannot carry authority into a different host.
            await asyncio.to_thread(manager.clear_native_input_handoff, terminal.id, attachment_id)
            deadlines.pop(key, None)
            continue
        deadline = deadlines.setdefault(key, now + NATIVE_INPUT_REBIND_SECONDS)
        if now < deadline:
            continue
        try:
            await client.revoke_input(host_terminal_id, attachment_id)
        except _HOST_FAILURES as exc:
            if not (isinstance(exc, HostCommandError) and exc.code == "not_found"):
                continue
        await asyncio.to_thread(manager.clear_native_input_handoff, terminal.id, attachment_id)
        deadlines.pop(key, None)
    for key in deadlines.keys() - present:
        del deadlines[key]


__all__ = ["InputGrantRuntime", "LeaseHolder", "sync_host_input_grant"]
