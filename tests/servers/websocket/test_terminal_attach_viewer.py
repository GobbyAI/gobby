"""A terminal attach is lease-free only when it names the gclient viewer."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.servers.websocket.terminal_ws import _attach_viewer


@pytest.mark.parametrize(
    ("data", "viewer"),
    [
        ({"viewer": "gclient"}, "gclient"),
        ({"viewer": "web"}, "web"),
        ({}, "web"),
        ({"viewer": "Gclient"}, "web"),
        ({"viewer": "anything-else"}, "web"),
    ],
)
def test_attach_viewer_is_gclient_only_when_named(data: dict[str, Any], viewer: str) -> None:
    assert _attach_viewer(data) == viewer
