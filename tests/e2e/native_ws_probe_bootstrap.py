"""Runner wrapper that records each successful native bind for the e2e probe.

The genuine runner runs unchanged inside the backend process. The wrapper only
observes `NativePublisher.bind` after it succeeds, and appends the bound
generation with the proof credentials the backend holds, so a test can drive
the private bind and publish routes the way gdaemon would. Nothing is recorded
for a refused or failed bind.
"""

from __future__ import annotations

import os
import runpy
from pathlib import Path
from unittest.mock import patch

from gobby.servers.websocket.native_publish import NativePublisher
from tests.fixtures.native_ws_probe_records import recording_bind


def main() -> None:
    if os.environ.get("GOBBY_TEST_PROTECT") != "1":
        raise RuntimeError("The native WS probe requires an isolated test process")
    home = Path(os.environ["GOBBY_HOME"])
    with patch.object(NativePublisher, "bind", recording_bind(NativePublisher.bind, home)):
        runpy.run_module("gobby.runner", run_name="__main__")


if __name__ == "__main__":
    main()
