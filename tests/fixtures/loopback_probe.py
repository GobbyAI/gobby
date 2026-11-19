"""One TCP connect, classified, for the loopback-only proof.

Usage: python loopback_probe.py <host:port> <out-file>

Connects once with a 5 second timeout and writes
{"errno": <int or null>, "class": "connected" | "eperm" | "refused" | "timeout" | "other"}
to <out-file>. The harness reads the class. Only "connected" and "eperm" are conclusive; a
harness test never passes on an inconclusive class.
"""

from __future__ import annotations

import errno
import json
import socket
import sys
from pathlib import Path

TIMEOUT_SECONDS = 5.0
USAGE = "usage: loopback_probe.py <host:port> <out-file>"


def parse_target(target: str) -> tuple[str, int]:
    host, separator, port_text = target.rpartition(":")
    if not separator or not host:
        raise ValueError(f"target must be host:port, got {target!r}")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range: {port}")
    return host, port


def classify(error: BaseException) -> tuple[int | None, str]:
    if isinstance(error, TimeoutError):
        return None, "timeout"
    if isinstance(error, OSError):
        if error.errno == errno.EPERM:
            return error.errno, "eperm"
        if error.errno == errno.ECONNREFUSED:
            return error.errno, "refused"
        return error.errno, "other"
    return None, "other"


def probe(host: str, port: int) -> dict[str, int | str | None]:
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT_SECONDS):
            return {"errno": None, "class": "connected"}
    except OSError as error:
        number, label = classify(error)
        return {"errno": number, "class": label}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        host, port = parse_target(argv[1])
    except ValueError as error:
        print(f"loopback_probe: {error}", file=sys.stderr)
        return 2
    Path(argv[2]).write_text(json.dumps(probe(host, port)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
