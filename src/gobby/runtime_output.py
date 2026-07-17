"""Raw process-output helpers for the daemon runtime stream."""

import sys


def forward_subprocess_stderr(stderr: bytes | str) -> str:
    """Forward captured subprocess stderr to the daemon stderr stream once."""
    decoded = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
    text = decoded.strip()
    if text:
        sys.stderr.write(f"{text}\n")
        sys.stderr.flush()
    return text
