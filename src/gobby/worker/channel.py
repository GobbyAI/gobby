"""The worker channel: the one inherited socket a Rust-mode gdaemon gives its Python worker.

Stub stage. Every entry point has its final signature and returns the unimplemented
answer until the implementation replaces it.
"""

from __future__ import annotations

import asyncio
import os
import struct
from dataclasses import dataclass

WORKER_MARKER_ENV = "GOBBY_WORKER_MARKER"
WORKER_CHANNEL_FD_ENV = "GOBBY_WORKER_CHANNEL_FD"
WORKER_CREDENTIAL_FD_ENV = "GOBBY_WORKER_CREDENTIAL_FD"

FRAME_PREFIX = struct.Struct(">I")
MAX_FRAME_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class WorkerBoot:
    """The inherited channel descriptor and the per-spawn credential of a marked start."""

    channel_fd: int
    credential: str


def take_worker_boot() -> WorkerBoot | None:
    """Return the worker boot for a marked start, or None for an unmarked start."""
    return None


async def read_frame(reader: asyncio.StreamReader, limit: int) -> bytes | None:
    """Read one length-prefixed frame, or None at end of stream."""
    return None


def encode_frame(payload: bytes) -> bytes:
    """Prefix a payload with its big-endian length."""
    return b""


async def serve_worker(boot: WorkerBoot) -> None:
    """Take the channel descriptor and give it up without reading a request."""
    os.close(boot.channel_fd)


async def _no_lifecycle_phase() -> object:
    """The create phase of the worker-only boot. No lifecycle phase is registered yet."""
    raise RuntimeError("the worker-only boot registers no lifecycle phase")
