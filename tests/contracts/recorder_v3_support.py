"""Leaf helpers for the v3 recorder: the REST tool sender, the provider-edge capture and the error.

This module imports nothing from ``recorder_v3``, so the recorder and its tests can both load it
without a cycle. The bodies here are signature-level stubs: each returns an inert value and never
raises, so the tests that use them fail on their assertions until the behaviour lands.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx


class RecorderError(RuntimeError):
    """A recording or replay could not run to a comparable result."""


def send_mcp_rest(
    client: httpx.Client, invocation: Mapping[str, Any], *, operator: Mapping[str, str]
) -> Any:
    """Stub: POST the invocation's arguments to its REST tool route and return the JSON body."""
    del client, invocation, operator
    return {}


class ProviderEdgeCapture:
    """Stub: the listed-host hits of one provider-edge log, in arrival order."""

    def __init__(self, edge: Any, hosts: Sequence[str]) -> None:
        self._edge = edge
        self._hosts = tuple(hosts)

    def hits(self) -> list[dict[str, Any]]:
        return []
