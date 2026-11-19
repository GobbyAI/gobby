"""Loopback HTTPS CONNECT proxy that stands in for the provider hosts the comms adapters reach.

The edge terminates TLS with a test certificate authority, mints a leaf certificate for each
CONNECT host on demand, answers each provider host from its scripted replies, and never forwards
upstream. It logs every arrival in order, so a capture can read the hosts a request reached.

This module is a stub: the signatures are final, and every member returns an inert value until the
edge is built.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

EDGE_DIR = Path(__file__).resolve().parent / "edge"


class FakeEdge:
    """A loopback provider edge, owned by one recorder run or one test."""

    def __init__(self, root: Path) -> None:
        """Keep the test authority and its key under ``root``. Nothing is bound until ``start``."""
        self._root = root

    def start(self) -> None:
        """Create the test authority and bind the loopback listeners."""

    def stop(self) -> None:
        """Close every listener."""

    @property
    def address(self) -> tuple[str, int]:
        """The bound HTTPS CONNECT proxy address, as (host, port)."""
        return ("", 0)

    def listener(self, protocol: str) -> tuple[str, int]:
        """The bound address of an email listener, ``imap`` or ``smtp``, as (host, port)."""
        return ("", 0)

    def ca_path(self) -> Path:
        """The PEM file of the test authority that clients must trust."""
        return Path("")

    def hits(self) -> list[dict[str, Any]]:
        """Every arrival since the last clear, in arrival order, as host, method and path."""
        return []

    def clear(self) -> None:
        """Forget every arrival so far."""
