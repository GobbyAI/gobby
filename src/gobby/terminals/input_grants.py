"""Make the gterm frame-stream input grant follow the daemon's writer lease.

gclient types straight into the host once the daemon has granted its attachment
(memory b59e4ce9). The daemon still decides who may write: every lease transition
reconciles the host grant with the holder through :func:`sync_host_input_grant`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from gobby.terminals.host_client import HostCommandError, HostEpochChangedError
from gobby.terminals.runtime import TerminalWriteError

if TYPE_CHECKING:
    from gobby.storage.terminals import Terminal

logger = logging.getLogger(__name__)


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
) -> bool | None:
    """Reconcile the host grant with the lease holder after one transition.

    Returns ``None`` when no grant applies: the terminal is not native, or the
    holder is not a direct viewer (a web or proxied holder, or no holder), in which
    case any standing grant is revoked first. Returns ``True`` when the host
    accepted the grant and ``False`` when it refused or could not be reached.
    """
    if terminal is None or terminal.backend != "native":
        return None
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
    return True


__all__ = ["InputGrantRuntime", "LeaseHolder", "sync_host_input_grant"]
