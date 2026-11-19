"""The worker channel: the one inherited socket a Rust-mode gdaemon gives its Python worker.

A marked start carries three variables that only the Rust supervisor sets: the marker, the
channel descriptor (one end of a connected socket pair) and the credential descriptor (a pipe
the supervisor fills and closes before spawn). ``take_worker_boot`` consumes them. The worker
then serves the facade over the socket with length-prefixed frames: a 4-byte big-endian length
and the payload. A frame whose length is over the limit is refused before its body is read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import struct
from dataclasses import dataclass, field

from gobby.runner_init import StartRefusal

logger = logging.getLogger(__name__)

WORKER_MARKER_ENV = "GOBBY_WORKER_MARKER"
WORKER_CHANNEL_FD_ENV = "GOBBY_WORKER_CHANNEL_FD"
WORKER_CREDENTIAL_FD_ENV = "GOBBY_WORKER_CREDENTIAL_FD"

FRAME_PREFIX = struct.Struct(">I")
MAX_FRAME_BYTES = 16 * 1024 * 1024

# Descriptors 0 to 2 are the standard streams, so a channel never uses one of them.
_LOWEST_INHERITED_FD = 3


class FrameError(Exception):
    """A frame the channel refuses. The channel closes after it."""


@dataclass(frozen=True)
class WorkerBoot:
    """The inherited channel descriptor and the per-spawn credential of a marked start.

    The credential is left out of the repr, so a logged boot does not carry it.
    """

    channel_fd: int
    credential: str = field(repr=False)


def take_worker_boot() -> WorkerBoot | None:
    """Consume the worker variables. Return None for an unmarked start, else the boot.

    All three variables leave the environment. The credential descriptor is read to end of
    stream and closed here. The channel descriptor stays open for ``serve_worker``.
    """
    marker = os.environ.pop(WORKER_MARKER_ENV, None)
    channel_text = os.environ.pop(WORKER_CHANNEL_FD_ENV, None)
    credential_text = os.environ.pop(WORKER_CREDENTIAL_FD_ENV, None)
    if marker is None:
        return None
    if marker != "1":
        raise StartRefusal(f"{WORKER_MARKER_ENV} must be 1 when it is set")
    if os.name == "nt":
        raise StartRefusal("worker channel needs unix descriptors")
    channel_fd = _inherited_descriptor(WORKER_CHANNEL_FD_ENV, channel_text)
    credential_fd = _inherited_descriptor(WORKER_CREDENTIAL_FD_ENV, credential_text)
    return WorkerBoot(channel_fd=channel_fd, credential=_read_credential(credential_fd))


def _inherited_descriptor(name: str, text: str | None) -> int:
    """The descriptor a variable names. It must be an inherited descriptor above stderr."""
    try:
        fd = int(text or "")
    except ValueError:
        fd = -1
    if fd < _LOWEST_INHERITED_FD:
        raise StartRefusal(f"{name} must name an inherited descriptor above stderr")
    return fd


def _read_credential(fd: int) -> str:
    """Read the credential to end of stream from its own descriptor, then close it."""
    try:
        with os.fdopen(fd, "rb") as pipe:
            payload = pipe.read()
    except OSError as exc:
        raise StartRefusal(
            f"{WORKER_CREDENTIAL_FD_ENV} names a descriptor that cannot be read"
        ) from exc
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StartRefusal(f"{WORKER_CREDENTIAL_FD_ENV} does not carry UTF-8 text") from exc


async def read_frame(reader: asyncio.StreamReader, limit: int) -> bytes | None:
    """Read one frame. None means the peer closed before a whole prefix or body arrived.

    A length prefix over ``limit`` raises FrameError before any body byte is read.
    """
    try:
        prefix = await reader.readexactly(FRAME_PREFIX.size)
    except asyncio.IncompleteReadError:
        return None
    (size,) = FRAME_PREFIX.unpack(prefix)
    if size > limit:
        raise FrameError(f"frame of {size} bytes is over the {limit}-byte limit")
    try:
        return await reader.readexactly(size)
    except asyncio.IncompleteReadError:
        return None


def encode_frame(payload: bytes) -> bytes:
    """Prefix a payload with its big-endian length."""
    if len(payload) > MAX_FRAME_BYTES:
        raise FrameError(f"reply of {len(payload)} bytes is over the {MAX_FRAME_BYTES}-byte limit")
    return FRAME_PREFIX.pack(len(payload)) + payload


async def serve_worker(boot: WorkerBoot) -> None:
    """Serve the worker facade over the channel until the peer closes it or breaks framing.

    Each request frame gets exactly one reply frame. The channel is closed on return.
    """
    # Imported here so that a start which takes no worker boot never loads the facade chain.
    from gobby.worker.facade import WorkerFacade

    facade = WorkerFacade(credential=boot.credential, create=_no_lifecycle_phase)
    reader, writer = await asyncio.open_connection(sock=socket.socket(fileno=boot.channel_fd))
    try:
        while (request := await read_frame(reader, MAX_FRAME_BYTES)) is not None:
            writer.write(encode_frame(await facade.handle(request)))
            await writer.drain()
    except (FrameError, ConnectionError) as exc:
        logger.warning("worker channel closed: %s", exc)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def _no_lifecycle_phase() -> object:
    """The create phase of the worker-only boot. No lifecycle phase is registered yet."""
    raise RuntimeError("the worker-only boot registers no lifecycle phase")
