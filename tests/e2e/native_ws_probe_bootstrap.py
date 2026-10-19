"""Runner wrapper that records each successful native bind for the e2e probe.

The genuine runner runs unchanged inside the backend process. The wrapper only
observes `NativePublisher.bind` after it succeeds, and appends the bound
generation with the proof credentials the backend holds, so a test can drive
the private bind and publish routes the way gdaemon would. Nothing is recorded
for a refused or failed bind.
"""

from __future__ import annotations

import json
import os
import runpy
from pathlib import Path
from unittest.mock import patch

from gobby.runner_backend import supervisor_write_token
from gobby.servers.websocket.native_publish import NativePublisher

RECORDS_FILE = "native-ws-binds.jsonl"


def _append_record(home: Path, record: dict[str, object]) -> None:
    fd = os.open(home / RECORDS_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as handle:
        handle.write(json.dumps(record) + "\n")


def main() -> None:
    if os.environ.get("GOBBY_TEST_PROTECT") != "1":
        raise RuntimeError("The native WS probe requires an isolated test process")
    home = Path(os.environ["GOBBY_HOME"])
    original = NativePublisher.bind

    async def bind(self: NativePublisher) -> str:
        generation = await original(self)
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

    with patch.object(NativePublisher, "bind", bind):
        runpy.run_module("gobby.runner", run_name="__main__")


if __name__ == "__main__":
    main()
