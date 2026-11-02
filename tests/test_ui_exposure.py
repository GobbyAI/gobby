"""Tailscale exposure: adoption, refusal, rollback, probe failures, and installer consent."""

from __future__ import annotations

import copy
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from gobby import ui_exposure
from gobby.config.app import McpExposureConfig, UiExposureConfig
from gobby.ui_exposure import UiExposeError

pytestmark = pytest.mark.unit

DAEMON_PORT = 60887
DNS_NAME = "node.example.ts.net"
ROOT_PROXY = f"http://localhost:{DAEMON_PORT}"
MCP_PROXY = f"http://localhost:{DAEMON_PORT}/mcp"
ROOT_HOST_PORT = f"{DNS_NAME}:443"

MutationHook = Callable[["FakeTailscale", list[str]], None]


class FakeTailscale:
    """In-memory tailscale CLI with injectable serve-status failures and mutation hooks."""

    def __init__(self) -> None:
        self.dns_name = DNS_NAME
        self.status: dict[str, Any] = {
            "BackendState": "Running",
            "CurrentTailnet": {"MagicDNSEnabled": True},
            "Self": {"DNSName": f"{DNS_NAME}."},
        }
        self.serve: dict[str, Any] = {"TCP": {}, "Web": {}, "AllowFunnel": {}}
        self.mutations: list[list[str]] = []
        self.serve_reads = 0
        self.failing_serve_reads: set[int] = set()
        self.on_mutation: MutationHook | None = None

    def run_json(self, arguments: list[str]) -> dict[str, object]:
        if arguments == ["status", "--json"]:
            return copy.deepcopy(self.status)
        assert arguments == ["serve", "status", "--json"], arguments
        self.serve_reads += 1
        if self.serve_reads in self.failing_serve_reads:
            raise UiExposeError("tailscale command timed out")
        return copy.deepcopy(self.serve)

    def run_mutation(self, arguments: list[str]) -> None:
        self.mutations.append(arguments)
        if self.on_mutation is not None:
            self.on_mutation(self, arguments)
        else:
            self.apply(arguments)

    def apply(self, arguments: list[str]) -> None:
        https = _flag(arguments, "--https=")
        set_path = _flag(arguments, "--set-path=")
        host_port = f"{self.dns_name}:{https}"
        off = arguments[-1] == "off"
        web = self.serve["Web"]
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


def _root_proxy(fake: FakeTailscale) -> str | None:
    handler = fake.serve["Web"].get(ROOT_HOST_PORT, {}).get("Handlers", {}).get("/")
    return None if handler is None else handler["Proxy"]


def _raise(message: str) -> Callable[[], None]:
    def persist() -> None:
        raise RuntimeError(message)

    return persist


def _refuse_any_call(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("Tailscale must not be called")


@pytest.fixture
def tailscale(monkeypatch: pytest.MonkeyPatch) -> FakeTailscale:
    fake = FakeTailscale()
    monkeypatch.setattr(ui_exposure, "_run_json", fake.run_json)
    monkeypatch.setattr(ui_exposure, "_run_mutation", fake.run_mutation)
    return fake


def test_enable_installs_root_then_records_intent(tailscale: FakeTailscale) -> None:
    recorded_after: list[int] = []

    result = ui_exposure.enable_tailscale_ui(
        DAEMON_PORT, persist=lambda: recorded_after.append(len(tailscale.mutations))
    )

    assert result.enabled is True
    assert result.changed is True
    assert result.url == f"https://{DNS_NAME}/"
    assert _root_proxy(tailscale) == ROOT_PROXY
    assert recorded_after == [1]


def test_enable_adopts_equivalent_manual_mapping_without_mutation(
    tailscale: FakeTailscale,
) -> None:
    tailscale.serve = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {ROOT_HOST_PORT: {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{DAEMON_PORT}/"}}}},
        "AllowFunnel": {},
    }
    recorded: list[bool] = []

    result = ui_exposure.enable_tailscale_ui(DAEMON_PORT, persist=lambda: recorded.append(True))

    assert result.changed is False
    assert tailscale.mutations == []
    assert recorded == [True]


def test_enable_fails_closed_on_foreign_root_handler(tailscale: FakeTailscale) -> None:
    tailscale.serve = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {ROOT_HOST_PORT: {"Handlers": {"/": {"Proxy": "http://localhost:9999"}}}},
        "AllowFunnel": {},
    }

    with pytest.raises(UiExposeError, match="owned by another proxy"):
        ui_exposure.enable_tailscale_ui(DAEMON_PORT)

    assert tailscale.mutations == []


def test_enable_refuses_port_with_conflicting_protocol_state(tailscale: FakeTailscale) -> None:
    tailscale.serve = {
        "TCP": {"443": {"TCP": {"ProxyProtocol": 1}}},
        "Web": {},
        "AllowFunnel": {},
    }

    with pytest.raises(UiExposeError, match="conflicting protocol state"):
        ui_exposure.enable_tailscale_ui(DAEMON_PORT)

    assert tailscale.mutations == []


def test_enable_verification_failure_keeps_foreign_root(tailscale: FakeTailscale) -> None:
    def replaced_by_someone_else(fake: FakeTailscale, _arguments: list[str]) -> None:
        fake.serve["TCP"]["443"] = {"HTTPS": True}
        fake.serve["Web"][ROOT_HOST_PORT] = {"Handlers": {"/": {"Proxy": "http://localhost:9999"}}}

    tailscale.on_mutation = replaced_by_someone_else

    with pytest.raises(UiExposeError, match="did not install the expected HTTPS / handler"):
        ui_exposure.enable_tailscale_ui(DAEMON_PORT)

    assert len(tailscale.mutations) == 1
    assert _root_proxy(tailscale) == "http://localhost:9999"


def test_enable_inconclusive_verification_removes_confirmed_new_root(
    tailscale: FakeTailscale,
) -> None:
    tailscale.failing_serve_reads = {2}

    with pytest.raises(UiExposeError, match="timed out"):
        ui_exposure.enable_tailscale_ui(DAEMON_PORT)

    assert tailscale.mutations[-1][-1] == "off"
    assert _root_proxy(tailscale) is None


def test_enable_persistence_failure_rolls_back_new_root(tailscale: FakeTailscale) -> None:
    with pytest.raises(UiExposeError, match="failed to persist exposure intent: disk full"):
        ui_exposure.enable_tailscale_ui(DAEMON_PORT, persist=_raise("disk full"))

    assert tailscale.mutations[-1][-1] == "off"
    assert _root_proxy(tailscale) is None


def test_disable_removes_root_then_records_intent_off(tailscale: FakeTailscale) -> None:
    ui_exposure.enable_tailscale_ui(DAEMON_PORT)
    root_present_at_record: list[bool] = []

    result = ui_exposure.disable_tailscale_ui(
        DAEMON_PORT,
        persist=lambda: root_present_at_record.append(ROOT_HOST_PORT in tailscale.serve["Web"]),
    )

    assert result.enabled is False
    assert result.changed is True
    assert root_present_at_record == [False]


def test_disable_persistence_failure_restores_root(tailscale: FakeTailscale) -> None:
    ui_exposure.enable_tailscale_ui(DAEMON_PORT)

    with pytest.raises(UiExposeError, match="failed to persist exposure intent"):
        ui_exposure.disable_tailscale_ui(DAEMON_PORT, persist=_raise("disk full"))

    assert _root_proxy(tailscale) == ROOT_PROXY


def test_disable_persistence_failure_keeps_foreign_root_written_meanwhile(
    tailscale: FakeTailscale,
) -> None:
    ui_exposure.enable_tailscale_ui(DAEMON_PORT)

    def foreign_then_fail() -> None:
        tailscale.serve["Web"][ROOT_HOST_PORT] = {
            "Handlers": {"/": {"Proxy": "http://localhost:9999"}}
        }
        raise RuntimeError("disk full")

    with pytest.raises(UiExposeError, match="failed to persist exposure intent"):
        ui_exposure.disable_tailscale_ui(DAEMON_PORT, persist=foreign_then_fail)

    assert _root_proxy(tailscale) == "http://localhost:9999"


def test_forget_clears_intent_without_tailscale_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui_exposure, "_run_json", _refuse_any_call)
    monkeypatch.setattr(ui_exposure, "_run_mutation", _refuse_any_call)
    recorded: list[bool] = []

    result = ui_exposure.disable_tailscale_ui(
        DAEMON_PORT, forget=True, persist=lambda: recorded.append(True)
    )

    assert result.enabled is False
    assert recorded == [True]


def test_status_and_reconcile_skip_tailscale_without_recorded_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ui_exposure, "_run_json", _refuse_any_call)
    monkeypatch.setattr(ui_exposure, "_run_mutation", _refuse_any_call)

    results = ui_exposure.reconcile_exposures(
        DAEMON_PORT, ui=UiExposureConfig(), mcp=McpExposureConfig()
    )
    ui_status, mcp_status = ui_exposure.get_exposure_status(
        DAEMON_PORT, ui=UiExposureConfig(), mcp=McpExposureConfig()
    )

    assert results == []
    assert ui_status.enabled is False
    assert mcp_status.enabled is False


def test_status_reports_healthy_and_degraded_exposures(tailscale: FakeTailscale) -> None:
    ui_exposure.enable_tailscale_ui(DAEMON_PORT, port=8443)
    ui_exposure.enable_mcp_exposure(DAEMON_PORT, mode="funnel", port=443)
    recorded = (
        UiExposureConfig(mode="tailnet", port=8443),
        McpExposureConfig(mode="funnel", port=443),
    )

    healthy_ui, healthy_mcp = ui_exposure.get_exposure_status(
        DAEMON_PORT, ui=recorded[0], mcp=recorded[1]
    )
    tailscale.serve["AllowFunnel"] = {}
    degraded_ui, degraded_mcp = ui_exposure.get_exposure_status(
        DAEMON_PORT, ui=recorded[0], mcp=recorded[1]
    )

    assert healthy_ui.degraded_reason is None
    assert healthy_mcp.degraded_reason is None
    assert healthy_mcp.funnel is True
    assert degraded_ui.degraded_reason is None
    assert degraded_mcp.degraded_reason == "Tailscale Funnel is not active on port 443"


@pytest.mark.parametrize(
    ("status_patch", "message"),
    [
        ({"BackendState": "NeedsLogin"}, "Tailscale backend is not running"),
        ({"CurrentTailnet": {"MagicDNSEnabled": False}}, "Tailscale MagicDNS is disabled"),
    ],
)
def test_node_probe_fails_closed(
    tailscale: FakeTailscale, status_patch: dict[str, Any], message: str
) -> None:
    tailscale.status.update(status_patch)

    with pytest.raises(UiExposeError, match=message):
        ui_exposure.enable_tailscale_ui(DAEMON_PORT)

    assert tailscale.mutations == []


def test_node_probe_without_dns_name_fails_closed(tailscale: FakeTailscale) -> None:
    tailscale.status["Self"] = {}

    with pytest.raises(UiExposeError, match="missing this node's MagicDNS name"):
        ui_exposure.enable_tailscale_ui(DAEMON_PORT)

    assert tailscale.mutations == []


def test_json_probe_reports_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    with pytest.raises(UiExposeError, match="tailscale executable was not found"):
        ui_exposure._run_json(["status", "--json"])


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "message"),
    [
        (1, "", "not logged in", "tailscale command failed: not logged in"),
        (0, "not json", "", "tailscale returned malformed JSON"),
        (0, "[]", "", "tailscale returned malformed JSON"),
    ],
)
def test_json_probe_reports_command_and_parse_failures(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    stderr: str,
    message: str,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/tailscale")
    completed = subprocess.CompletedProcess(
        args=["tailscale"], returncode=returncode, stdout=stdout, stderr=stderr
    )
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: completed)

    with pytest.raises(UiExposeError, match=message):
        ui_exposure._run_json(["status", "--json"])


def test_mutation_is_blocked_by_test_protect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/tailscale")
    monkeypatch.setattr(subprocess, "run", _refuse_any_call)

    with pytest.raises(UiExposeError, match="blocked by GOBBY_TEST_PROTECT"):
        ui_exposure._run_mutation(["serve", "--yes", "--https=443", "--set-path=/", "off"])


@pytest.mark.parametrize(
    ("route", "target", "expected"),
    [
        (ui_exposure._ui_route(443), ROOT_PROXY, True),
        (ui_exposure._ui_route(443), f"http://127.0.0.1:{DAEMON_PORT}/", True),
        (ui_exposure._ui_route(443), MCP_PROXY, False),
        (ui_exposure._ui_route(443), f"https://localhost:{DAEMON_PORT}", False),
        (ui_exposure._ui_route(443), f"http://localhost:{DAEMON_PORT + 1}", False),
        (ui_exposure._ui_route(443), f"{ROOT_PROXY}?x=1", False),
        (ui_exposure._mcp_route(443, mode="tailnet"), MCP_PROXY, True),
        (ui_exposure._mcp_route(443, mode="tailnet"), ROOT_PROXY, False),
        (ui_exposure._mcp_route(443, mode="tailnet"), f"{MCP_PROXY}/", False),
    ],
)
def test_proxy_target_comparison_is_semantic(
    route: ui_exposure._Route, target: str, expected: bool
) -> None:
    assert ui_exposure._is_gobby_target(target, route, DAEMON_PORT) is expected


def test_installer_consent_is_tri_state(monkeypatch: pytest.MonkeyPatch) -> None:
    def consent_not_asked() -> bool:
        raise AssertionError("consent must not be asked")

    def node_reachable() -> ui_exposure._NodeInfo:
        return ui_exposure._NodeInfo(dns_name=DNS_NAME)

    def node_unreachable() -> ui_exposure._NodeInfo:
        raise UiExposeError("Tailscale backend is not running")

    def resolve(
        explicit: bool | None, *, full: bool, interactive: bool, confirm: Callable[[], bool]
    ) -> bool:
        return ui_exposure.resolve_installer_ui_exposure(
            explicit,
            full_install=full,
            no_interactive=not interactive,
            confirm=confirm,
        )

    monkeypatch.setattr(ui_exposure, "_read_node_info", node_reachable)
    assert resolve(True, full=False, interactive=False, confirm=consent_not_asked) is True
    assert resolve(False, full=True, interactive=True, confirm=consent_not_asked) is False
    assert resolve(None, full=False, interactive=True, confirm=consent_not_asked) is False
    assert resolve(None, full=True, interactive=False, confirm=consent_not_asked) is False
    assert resolve(None, full=True, interactive=True, confirm=lambda: True) is True
    assert resolve(None, full=True, interactive=True, confirm=lambda: False) is False

    monkeypatch.setattr(ui_exposure, "_read_node_info", node_unreachable)
    assert resolve(None, full=True, interactive=True, confirm=consent_not_asked) is False


def test_installer_declined_applies_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui_exposure, "_run_json", _refuse_any_call)
    monkeypatch.setattr(ui_exposure, "_run_mutation", _refuse_any_call)

    assert ui_exposure.apply_installer_ui_exposure(False, DAEMON_PORT) is None
