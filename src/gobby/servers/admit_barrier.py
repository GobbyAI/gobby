"""Test-only admit barrier used by isolated e2e lease-drain cases.

Production request handling never imports this module. Runner bind-time
wiring attaches ``await_test_admit_barrier`` to ``EffectFence.admit_hook``
only when ``GOBBY_TEST_PROTECT=1``.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path


async def await_test_admit_barrier() -> None:
    """Yield after admission when an isolated e2e barrier file is present."""
    if os.environ.get("GOBBY_TEST_PROTECT") != "1":
        return
    home = os.environ.get("GOBBY_HOME")
    if not home:
        return
    flag = Path(home) / "runtime" / "e2e-admit-barrier"
    if not flag.is_file():
        return
    admitted = flag.with_name("e2e-admit-barrier.admitted")
    release = flag.with_name("e2e-admit-barrier.release")
    admitted.write_text("1")
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if release.is_file():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("e2e admit barrier was not released")
