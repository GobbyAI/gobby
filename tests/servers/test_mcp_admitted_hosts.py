"""The node's Tailscale name is admitted as an MCP Host only while /mcp is exposed."""

from __future__ import annotations

from typing import Literal

import pytest

from gobby.config.app import McpExposureConfig
from gobby.servers.app_factory import _admitted_mcp_hosts
from gobby.ui_exposure import UiExposeError

pytestmark = pytest.mark.unit

NODE = "node.example.ts.net"
CONFIGURED = ["configured.example"]


@pytest.mark.parametrize(
    ("mode", "port", "expected"),
    [
        ("off", 443, CONFIGURED),
        ("tailnet", 443, [*CONFIGURED, NODE]),
        ("funnel", 443, [*CONFIGURED, NODE]),
        ("funnel", 8443, [*CONFIGURED, f"{NODE}:8443"]),
    ],
)
def test_exposed_node_name_is_admitted_with_its_port(
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["off", "tailnet", "funnel"],
    port: int,
    expected: list[str],
) -> None:
    monkeypatch.setattr("gobby.ui_exposure.node_dns_name", lambda: NODE)

    admitted = _admitted_mcp_hosts(CONFIGURED, McpExposureConfig(mode=mode, port=port))

    assert admitted == expected


def test_unreadable_node_name_admits_only_configured_hosts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def unavailable() -> str:
        raise UiExposeError("Tailscale backend is not running")

    monkeypatch.setattr("gobby.ui_exposure.node_dns_name", unavailable)

    admitted = _admitted_mcp_hosts(CONFIGURED, McpExposureConfig(mode="tailnet"))

    assert admitted == CONFIGURED
    assert "Tailscale node name unavailable" in caplog.text
