"""Recorded Tailscale UI and /mcp exposure: ports, Funnel guards, and daemon-start restore."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gobby import ui_exposure
from gobby.config.app import DaemonConfig, McpExposureConfig, UiExposureConfig
from gobby.config.bootstrap import BootstrapConfigError, bootstrap_from_mapping
from gobby.ui_exposure import UiExposeError

pytestmark = pytest.mark.unit

DAEMON_PORT = 60887
OLD_NAME = "old-node.example.ts.net"
NEW_NAME = "new-node.example.ts.net"


class FakeTailscale:
    """In-memory tailscale CLI. Serve and funnel mutations change the state it reports."""

    def __init__(self, *, dns_name: str, serve: dict[str, Any] | None = None) -> None:
        self.dns_name = dns_name
        self.serve: dict[str, Any] = serve or {"TCP": {}, "Web": {}, "AllowFunnel": {}}
        self.mutations: list[list[str]] = []

    def run_json(self, arguments: list[str]) -> dict[str, object]:
        if arguments == ["status", "--json"]:
            return {
                "BackendState": "Running",
                "CurrentTailnet": {"MagicDNSEnabled": True},
                "Self": {"DNSName": f"{self.dns_name}."},
            }
        assert arguments == ["serve", "status", "--json"], arguments
        return copy.deepcopy(self.serve)

    def run_mutation(self, arguments: list[str]) -> None:
        self.mutations.append(arguments)
        https = _flag(arguments, "--https=")
        set_path = _flag(arguments, "--set-path=")
        host_port = f"{self.dns_name}:{https}"
        web = self.serve["Web"]
        off = arguments[-1] == "off"
        handlers = web.setdefault(host_port, {"Handlers": {}})["Handlers"]
        if off:
            handlers.pop(set_path, None)
            if not handlers:
                web.pop(host_port, None)
        else:
            handlers[set_path] = {"Proxy": arguments[-1]}
            self.serve["TCP"][https] = {"HTTPS": True}
        if arguments[0] == "funnel":
            self.serve["AllowFunnel"][host_port] = not off


def _flag(arguments: list[str], prefix: str) -> str:
    return next(arg for arg in arguments if arg.startswith(prefix)).removeprefix(prefix)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeTailscale:
    tailscale = FakeTailscale(dns_name=OLD_NAME)
    monkeypatch.setattr(ui_exposure, "_run_json", tailscale.run_json)
    monkeypatch.setattr(ui_exposure, "_run_mutation", tailscale.run_mutation)
    monkeypatch.setattr(ui_exposure, "bootstrap_path", lambda: tmp_path / "bootstrap.yaml")
    return tailscale


def test_ui_expose_serves_root_on_configured_port(fake: FakeTailscale) -> None:
    persisted: list[str] = []

    result = ui_exposure.enable_tailscale_ui(
        DAEMON_PORT, port=8443, persist=lambda: persisted.append("ui")
    )

    assert result.enabled is True
    assert result.changed is True
    assert result.url == f"https://{OLD_NAME}:8443/"
    assert fake.serve["Web"][f"{OLD_NAME}:8443"]["Handlers"] == {
        "/": {"Proxy": f"http://localhost:{DAEMON_PORT}"}
    }
    assert f"{OLD_NAME}:443" not in fake.serve["Web"]
    assert persisted == ["ui"]


def test_mcp_funnel_serves_only_mcp_path_on_its_port(fake: FakeTailscale) -> None:
    result = ui_exposure.enable_mcp_exposure(DAEMON_PORT, mode="funnel", port=443)

    assert result.enabled is True
    assert result.funnel is True
    assert result.url == f"https://{OLD_NAME}/mcp"
    assert fake.serve["Web"][f"{OLD_NAME}:443"]["Handlers"] == {
        "/mcp": {"Proxy": f"http://localhost:{DAEMON_PORT}/mcp"}
    }
    assert fake.serve["AllowFunnel"][f"{OLD_NAME}:443"] is True


def test_non_mcp_route_on_funnel_port_is_refused(fake: FakeTailscale) -> None:
    fake.serve = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {
            f"{OLD_NAME}:443": {"Handlers": {"/": {"Proxy": f"http://localhost:{DAEMON_PORT}"}}}
        },
        "AllowFunnel": {f"{OLD_NAME}:443": True},
    }

    with pytest.raises(UiExposeError):
        ui_exposure.enable_tailscale_ui(DAEMON_PORT, port=443)
    with pytest.raises(UiExposeError):
        ui_exposure.enable_mcp_exposure(DAEMON_PORT, mode="funnel", port=443)
    assert fake.mutations == []


def test_config_refuses_ui_port_equal_to_funnel_port() -> None:
    with pytest.raises(ValidationError):
        DaemonConfig(
            ui_exposure=UiExposureConfig(mode="tailnet", port=443),
            mcp_exposure=McpExposureConfig(mode="funnel", port=443),
        )


def test_config_refuses_funnel_port_outside_tailscale_funnel_ports() -> None:
    with pytest.raises(ValidationError):
        DaemonConfig(mcp_exposure=McpExposureConfig(mode="funnel", port=9000))


def test_fresh_config_exposes_nothing_and_publishes_no_funnel() -> None:
    config = DaemonConfig()

    assert config.ui_exposure.mode == "off"
    assert config.mcp_exposure.mode == "off"


def test_reconcile_with_no_recorded_choice_touches_nothing(fake: FakeTailscale) -> None:
    results = ui_exposure.reconcile_exposures(
        DAEMON_PORT, ui=UiExposureConfig(), mcp=McpExposureConfig()
    )

    assert results == []
    assert fake.mutations == []


def test_reconcile_restores_recorded_exposures_on_new_node_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tailscale = FakeTailscale(
        dns_name=NEW_NAME,
        serve={
            "TCP": {"9000": {"HTTPS": True}},
            "Web": {
                f"{NEW_NAME}:9000": {"Handlers": {"/other": {"Proxy": "http://localhost:9999"}}}
            },
            "AllowFunnel": {},
        },
    )
    monkeypatch.setattr(ui_exposure, "_run_json", tailscale.run_json)
    monkeypatch.setattr(ui_exposure, "_run_mutation", tailscale.run_mutation)
    monkeypatch.setattr(ui_exposure, "bootstrap_path", lambda: tmp_path / "bootstrap.yaml")

    results = ui_exposure.reconcile_exposures(
        DAEMON_PORT,
        ui=UiExposureConfig(mode="tailnet", port=8443),
        mcp=McpExposureConfig(mode="funnel", port=443),
    )

    assert [result.degraded_reason for result in results] == [None, None]
    web = tailscale.serve["Web"]
    assert web[f"{NEW_NAME}:8443"]["Handlers"] == {
        "/": {"Proxy": f"http://localhost:{DAEMON_PORT}"}
    }
    assert web[f"{NEW_NAME}:443"]["Handlers"] == {
        "/mcp": {"Proxy": f"http://localhost:{DAEMON_PORT}/mcp"}
    }
    assert tailscale.serve["AllowFunnel"][f"{NEW_NAME}:443"] is True
    assert web[f"{NEW_NAME}:9000"]["Handlers"] == {"/other": {"Proxy": "http://localhost:9999"}}
    assert all(OLD_NAME not in " ".join(call) for call in tailscale.mutations)
    assert all("9000" not in " ".join(call) for call in tailscale.mutations)


def test_status_reports_both_exposures_and_funnel_state(fake: FakeTailscale) -> None:
    ui_exposure.enable_tailscale_ui(DAEMON_PORT, port=8443)
    ui_exposure.enable_mcp_exposure(DAEMON_PORT, mode="funnel", port=443)

    ui_result, mcp_result = ui_exposure.get_exposure_status(
        DAEMON_PORT,
        ui=UiExposureConfig(mode="tailnet", port=8443),
        mcp=McpExposureConfig(mode="funnel", port=443),
    )

    assert ui_result.url == f"https://{OLD_NAME}:8443/"
    assert ui_result.degraded_reason is None
    assert mcp_result.url == f"https://{OLD_NAME}/mcp"
    assert mcp_result.funnel is True
    assert mcp_result.degraded_reason is None


def test_node_dns_name_comes_from_tailscale_status(fake: FakeTailscale) -> None:
    assert ui_exposure.node_dns_name() == OLD_NAME


def test_bootstrap_rejects_retired_ui_expose_key() -> None:
    with pytest.raises(BootstrapConfigError, match="gobby ui expose"):
        bootstrap_from_mapping({"ui_expose": "tailscale"})
