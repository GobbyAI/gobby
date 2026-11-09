"""Records each successful native bind for the native WS probe.

The e2e runner wrapper and the native publish tests share this recorder, so
neither imports the other's module.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from gobby.runner_backend import supervisor_write_token
from gobby.servers.websocket.native_publish import NativePublisher

RECORDS_FILE = "native-ws-binds.jsonl"


def _append_record(home: Path, record: dict[str, object]) -> None:
    fd = os.open(home / RECORDS_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as handle:
        handle.write(json.dumps(record) + "\n")


def recording_bind(
    original: Callable[..., Awaitable[str]], home: Path
) -> Callable[[NativePublisher, str], Awaitable[str]]:
    """Wraps a publisher bind so each successful bind is recorded in `home`."""

    async def bind(self: NativePublisher, label: str = "bind") -> str:
        generation = await original(self, label)
        _append_record(
            home,
            {
                "pid": os.getpid(),
                "generation": generation,
                "token": supervisor_write_token(),
                "secret": self.front_door_secret,
            },
        )
        return generation

    return bind
