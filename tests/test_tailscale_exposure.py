"""Recorded Tailscale UI and /mcp exposure: ports, Funnel guards, and daemon-start restore."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from gobby import ui_exposure
from gobby.cli import exposure_config
from gobby.cli.ui import ui
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
        self.reads: list[list[str]] = []

    def run_json(self, arguments: list[str]) -> dict[str, object]:
        self.reads.append(arguments)
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
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeTailscale:
    tailscale = FakeTailscale(dns_name=OLD_NAME)
    monkeypatch.setattr(ui_exposure, "_run_json", tailscale.run_json)
    monkeypatch.setattr(ui_exposure, "_run_mutation", tailscale.run_mutation)
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
    monkeypatch: pytest.MonkeyPatch,
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


def test_mcp_expose_refuses_invalid_funnel_port_before_any_tailscale_call(
    fake: FakeTailscale,
) -> None:
    result = CliRunner().invoke(ui, ["mcp-expose", "--funnel", "--port", "9000"])

    assert result.exit_code != 0
    assert "cannot be published with Funnel" in result.output
    assert fake.reads == []
    assert fake.mutations == []


def test_moving_funnel_mcp_route_removes_the_old_public_handler(fake: FakeTailscale) -> None:
    ui_exposure.enable_mcp_exposure(DAEMON_PORT, mode="funnel", port=443)

    ui_exposure.replace_mcp_exposure(
        DAEMON_PORT,
        previous=McpExposureConfig(mode="funnel", port=443),
        mode="funnel",
        port=8443,
    )

    assert set(fake.serve["Web"]) == {f"{OLD_NAME}:8443"}
    assert {key for key, public in fake.serve["AllowFunnel"].items() if public} == {
        f"{OLD_NAME}:8443"
    }


def test_failed_move_restores_the_recorded_mcp_route(
    fake: FakeTailscale, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui_exposure.enable_mcp_exposure(DAEMON_PORT, mode="tailnet", port=443)
    apply_mutation = fake.run_mutation

    def refuse_new_port(arguments: list[str]) -> None:
        if "--https=8443" in arguments and arguments[-1] != "off":
            raise UiExposeError("tailscale refused the new port")
        apply_mutation(arguments)

    monkeypatch.setattr(ui_exposure, "_run_mutation", refuse_new_port)

    with pytest.raises(UiExposeError, match="refused the new port"):
        ui_exposure.replace_mcp_exposure(
            DAEMON_PORT,
            previous=McpExposureConfig(mode="tailnet", port=443),
            mode="funnel",
            port=8443,
        )

    handlers = fake.serve["Web"][f"{OLD_NAME}:443"]["Handlers"]
    assert handlers == {"/mcp": {"Proxy": f"http://localhost:{DAEMON_PORT}/mcp"}}
    assert set(fake.serve["Web"]) == {f"{OLD_NAME}:443"}


def test_funnel_upgrade_over_a_served_route_is_refused_without_mutation(
    fake: FakeTailscale,
) -> None:
    ui_exposure.enable_mcp_exposure(DAEMON_PORT, mode="tailnet", port=443)
    fake.mutations.clear()

    with pytest.raises(UiExposeError, match="before enabling Funnel"):
        ui_exposure.enable_mcp_exposure(DAEMON_PORT, mode="funnel", port=443)

    assert fake.mutations == []
    assert set(fake.serve["Web"]) == {f"{OLD_NAME}:443"}


def test_unrelated_non_proxy_handler_does_not_block_reconciliation(fake: FakeTailscale) -> None:
    fake.serve = {
        "TCP": {"10000": {"HTTPS": True}},
        "Web": {f"{OLD_NAME}:10000": {"Handlers": {"/": {"Text": "hello"}}}},
        "AllowFunnel": {},
    }

    results = ui_exposure.reconcile_exposures(
        DAEMON_PORT,
        ui=UiExposureConfig(mode="tailnet", port=8443),
        mcp=McpExposureConfig(mode="funnel", port=443),
    )

    assert [result.degraded_reason for result in results] == [None, None]


def test_status_lines_name_both_exposures_and_funnel_state(
    fake: FakeTailscale, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui_exposure.enable_tailscale_ui(DAEMON_PORT, port=8443)
    ui_exposure.enable_mcp_exposure(DAEMON_PORT, mode="funnel", port=443)
    monkeypatch.setattr(
        exposure_config,
        "read_exposure_config",
        lambda _path: (
            UiExposureConfig(mode="tailnet", port=8443),
            McpExposureConfig(mode="funnel", port=443),
        ),
    )

    assert exposure_config.exposure_status_lines(DAEMON_PORT) == [
        f"Web UI exposure: tailnet (https://{OLD_NAME}:8443/; Funnel off)",
        f"MCP exposure: funnel (https://{OLD_NAME}/mcp; Funnel on)",
    ]
