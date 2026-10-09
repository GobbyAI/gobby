"""Security validation for remotely bound web UI configuration."""

from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.ui import effective_ui_host, is_loopback_bind_host
from gobby.servers.http import HTTPServer

pytestmark = pytest.mark.unit


def test_default_daemon_config_keeps_ui_on_its_local_host() -> None:
    config = DaemonConfig()

    assert effective_ui_host(config.ui.host, config.bind_host) == config.ui.host


@pytest.mark.parametrize("bind_host", ["localhost", "127.0.0.1", "::1"])
def test_local_ui_host_stays_local_for_loopback_daemon_bind(bind_host: str) -> None:
    assert effective_ui_host("localhost", bind_host) == "localhost"


@pytest.mark.parametrize("bind_host", ["0.0.0.0", "100.64.0.1", "::"])
def test_local_ui_host_follows_external_daemon_bind(bind_host: str) -> None:
    assert effective_ui_host("localhost", bind_host) == bind_host


def test_explicit_ui_host_is_preserved() -> None:
    assert effective_ui_host("127.0.0.2", "0.0.0.0") == "127.0.0.2"


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "localhost.",
        "127.0.0.1",
        "127.255.255.254",
        "::1",
        "::ffff:127.0.0.1",
    ],
)
def test_loopback_bind_host_accepts_unambiguous_local_addresses(host: str) -> None:
    assert is_loopback_bind_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "::",
        "192.168.1.10",
        "2001:db8::1",
        "host.example",
        "localhost.example",
        " localhost",
        "localhost..",
    ],
)
def test_loopback_bind_host_rejects_wildcard_external_and_ambiguous_names(host: str) -> None:
    assert not is_loopback_bind_host(host)


def test_remote_ui_allows_external_bind() -> None:
    config = DaemonConfig(
        bind_host="0.0.0.0",
        ui={"enabled": True},
    )

    assert config.ui.enabled


def test_disabled_ui_allows_external_bind() -> None:
    config = DaemonConfig(
        bind_host="0.0.0.0",
        ui={"enabled": False},
    )

    assert not config.ui.enabled


def test_http_server_allows_remote_ui_with_mandatory_auth() -> None:
    services = ServiceContainer(
        database=MagicMock(),
        session_manager=MagicMock(),
        task_manager=MagicMock(),
    )

    server = HTTPServer(
        services=services,
        startup_config=DaemonConfig(ui={"enabled": True}),
        bootstrap_config=BootstrapConfig(bind_host="0.0.0.0"),
    )
    denial = {
        "error": (
            "Authentication required. CLI clients need an API key in bootstrap.yaml "
            "and gdaemon (run 'gobby auth login'). Browsers: log in."
        ),
        "code": "missing_auth",
    }

    unauthenticated = TestClient(server.app).get("/api/tasks")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == denial
