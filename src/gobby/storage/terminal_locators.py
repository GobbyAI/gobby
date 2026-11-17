"""Native terminal identity and request-time attach handles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from gobby.utils.machine_id import require_machine_id

FRAMES_SOCKET_NAME = "gterm-frames.sock"


class HostEpochMismatchError(RuntimeError):
    """A locator belongs to a different host generation."""


class TerminalNotReadyError(RuntimeError):
    """A terminal has not yet recorded its live host identity."""


class MachineOwnershipMismatchError(RuntimeError):
    """A terminal cannot be attached from another machine."""

    def __init__(self) -> None:
        super().__init__("machine_ownership_mismatch")


def native_locator_key(host_epoch: str, host_terminal_id: str) -> str:
    """Return the durable key for one native host terminal generation."""
    return f"native:{host_epoch}:{host_terminal_id}"


@dataclass(frozen=True)
class AttachLocator:
    """Request-time attach handle; native socket path is computed, never stored."""

    backend: Literal["native"]
    frame_host_epoch: str
    host_socket: str | None = None
    host_terminal_id: str | None = None
    socket_path: str | None = None
    pane_id: str | None = None
    server_pid: int | None = None
    server_start_time: int | None = None

    def is_valid_for_direct(self, backend: str) -> bool:
        return (
            self.backend == backend
            and all(
                isinstance(value, str) and bool(value)
                for value in (self.frame_host_epoch, self.host_socket, self.host_terminal_id)
            )
            and all(
                value is None
                for value in (
                    self.socket_path,
                    self.pane_id,
                    self.server_pid,
                    self.server_start_time,
                )
            )
        )

    def direct_block(self) -> dict[str, object]:
        return {
            "host_epoch": self.frame_host_epoch,
            "frame_socket_path": self.host_socket,
            "host_terminal_id": self.host_terminal_id,
            "pane": None,
        }


class TerminalLocatorRecord(Protocol):
    """Terminal fields required to build an attach locator."""

    @property
    def backend(self) -> str: ...

    @property
    def machine_id(self) -> str: ...

    @property
    def host_epoch(self) -> str | None: ...

    @property
    def locator(self) -> Mapping[str, object] | None: ...


def native_attach_locator(
    row: TerminalLocatorRecord,
    *,
    live_host_epoch: str,
    host_socket: str | None,
) -> AttachLocator:
    """Build a native attach locator after validating the live host identity."""
    if not row.host_epoch:
        raise TerminalNotReadyError("Terminal spawn has not persisted its host epoch")
    if row.host_epoch != live_host_epoch:
        raise HostEpochMismatchError("Live host epoch does not match the terminal row")
    host_terminal_id = None
    if row.locator is not None:
        host_terminal_id = str(row.locator["host_terminal_id"])
    return AttachLocator(
        backend="native",
        frame_host_epoch=str(row.host_epoch),
        host_socket=host_socket,
        host_terminal_id=host_terminal_id,
    )


def attach_locator_for_terminal(
    row: TerminalLocatorRecord,
    *,
    live_host_epoch: str,
    socket_dir: Path | str,
) -> AttachLocator:
    """Compute an attach handle from an already-loaded terminal row."""
    if row.machine_id != require_machine_id():
        raise MachineOwnershipMismatchError
    return native_attach_locator(
        row,
        live_host_epoch=live_host_epoch,
        host_socket=str(Path(socket_dir) / FRAMES_SOCKET_NAME),
    )


def _native_locator(locator: Mapping[str, object]) -> dict[str, str]:
    if "host_socket" in locator:
        raise ValueError("native locator must not include host_socket")
    extra = set(locator) - {"host_terminal_id"}
    if extra:
        raise ValueError("native locator may only contain host_terminal_id")
    return {"host_terminal_id": str(locator["host_terminal_id"])}
