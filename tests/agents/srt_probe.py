"""Keep Node startup outside the identity probe's test-controlled response budget."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch


@contextmanager
def ready_node_probes(*, program: str | None = None) -> Iterator[None]:
    """Wait for eval initialization through a pipe, including deliberately slow startup."""
    original_popen = subprocess.Popen
    delay_ms = int(os.environ.get("GOBBY_TEST_SRT_PROBE_DELAY_MS", "0"))

    def start(argv: Sequence[str], **options: Any) -> subprocess.Popen[bytes]:
        if "--eval" not in argv:
            return original_popen(argv, **options)
        command = list(argv)
        script_index = command.index("--eval") + 1
        read_fd, ready_fd = os.pipe()
        command[script_index] = (
            f"Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, {delay_ms});"
            "try {"
            + (program if program is not None else command[script_index])
            + f"}} finally {{ require('fs').writeSync({ready_fd}, '1');"
            + f"require('fs').closeSync({ready_fd}); }}"
        )
        try:
            try:
                process = original_popen(command, pass_fds=(ready_fd,), **options)
            finally:
                os.close(ready_fd)
            # EOF also synchronizes a launcher that exits (the nonzero case).
            try:
                if not os.read(read_fd, 1):
                    process.wait(timeout=5)
            except BaseException:
                process.kill()
                process.wait(timeout=5)
                raise
            return process
        finally:
            os.close(read_fd)

    with patch("subprocess.Popen", new=start):
        yield
