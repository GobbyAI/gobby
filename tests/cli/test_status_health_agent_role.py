"""Managed observability must not use the agent's restricted hub credentials."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.config.app import DaemonConfig
from gobby.runner_pid_file import ProbeState, SingletonProbe
from gobby.utils.status import RichStatusProbe

pytestmark = pytest.mark.unit


@pytest.fixture
def bootstrap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep bootstrap, tokens, PID inspection and HTTP isolated from the daemon."""
    path = tmp_path / "bootstrap.yaml"
    path.write_text(f"daemon_port: 61234\nfiles_home: {tmp_path}\n")
    path.chmod(0o600)
    for name in ("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", "GOBBY_DAEMON_URL", "GOBBY_DAEMON_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOBBY_AGENT_API_TOKEN", "test-run-capability")
    for module in ("gobby.cli.daemon", "gobby.cli.daemon_health"):
        monkeypatch.setattr(f"{module}.get_gobby_home", lambda: tmp_path)
        monkeypatch.setattr(
            f"{module}.probe_daemon_lock",
            MagicMock(return_value=SingletonProbe(ProbeState.DAEMON, pid=12345, role="daemon")),
        )
        monkeypatch.setattr(f"{module}._is_process_alive", lambda _pid: True)
        monkeypatch.setattr(f"{module}.get_service_status", lambda: {"installed": False})
    monkeypatch.setattr("gobby.cli.daemon._read_pid_file", lambda: 12345)
    monkeypatch.setattr("gobby.cli.daemon.get_port_listener_pid", lambda _port: None)
    return path


@pytest.fixture
def forbidden_config(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    config = MagicMock(side_effect=PermissionError("permission denied for table config_store"))
    database = MagicMock(side_effect=AssertionError("managed observability opened the hub"))
    monkeypatch.setattr("gobby.cli.runtime.CliRuntime.require_config", config)
    monkeypatch.setattr("gobby.cli.runtime.CliRuntime.require_database", database)
    return config, database


@pytest.fixture
def http_get(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    get = MagicMock(return_value=httpx.Response(200, json={"status": "ok"}))
    monkeypatch.setattr("httpx.get", get)
    return get


@pytest.mark.parametrize("command", ["status", "health"])
def test_status_and_health_under_agent_role_skip_config_store(
    command: str,
    bootstrap: Path,
    forbidden_config: tuple[MagicMock, MagicMock],
    http_get: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", str(bootstrap.parent / "grant.json"))

    result = CliRunner().invoke(cli, ["--config", str(bootstrap), command])

    assert result.exit_code == 0, result.output
    assert "Gobby daemon: healthy" in result.output
    http_get.assert_called_once_with(
        "http://127.0.0.1:61234/api/health",
        headers={"Authorization": "Bearer test-run-capability"},
        timeout=2.0,
    )
    for access in forbidden_config:
        access.assert_not_called()


@pytest.mark.parametrize("command", ["status", "health"])
def test_managed_observability_needs_no_local_pid_or_operator_token(
    command: str,
    bootstrap: Path,
    forbidden_config: tuple[MagicMock, MagicMock],
    http_get: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", str(bootstrap.parent / "grant.json"))
    monkeypatch.setenv("GOBBY_DAEMON_URL", "http://127.0.0.1:61235")
    probe = MagicMock(side_effect=AssertionError("managed seat inspected daemon PID lock"))
    token = MagicMock(side_effect=AssertionError("managed seat read operator token"))
    monkeypatch.setattr("gobby.cli.daemon.probe_daemon_lock", probe)
    monkeypatch.setattr("gobby.cli.daemon_health.probe_daemon_lock", probe)
    monkeypatch.setattr("gobby.utils.local_token.read_local_api_token", token)

    result = CliRunner().invoke(cli, [command])

    assert result.exit_code == 0, result.output
    assert "Gobby daemon: healthy" in result.output
    assert http_get.call_args.args == ("http://127.0.0.1:61235/api/health",)
    probe.assert_not_called()
    token.assert_not_called()
    for access in forbidden_config:
        access.assert_not_called()


@pytest.mark.parametrize("command", ["status", "health"])
def test_status_and_health_under_runtime_role_unchanged(
    command: str,
    bootstrap: Path,
    http_get: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MagicMock(return_value=DaemonConfig())
    monkeypatch.setattr("gobby.cli.runtime.CliRuntime.require_config", config)
    monkeypatch.setattr("gobby.cli.runtime.CliRuntime.require_database", MagicMock())
    monkeypatch.setattr(
        "gobby.storage.schema_divergence.collect_schema_heads",
        MagicMock(return_value=MagicMock(diverged=False)),
    )
    monkeypatch.setattr(
        "gobby.cli.daemon_health.collect_schema_heads",
        MagicMock(return_value=MagicMock(diverged=False)),
    )
    monkeypatch.setattr(
        "gobby.cli.daemon.fetch_rich_status",
        AsyncMock(return_value=RichStatusProbe(api_data={"process": {}}, health_confirmed=True)),
    )
    monkeypatch.setattr("gobby.utils.deps.collect_all_deps", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("gobby.utils.deps.check_config_mismatches", lambda _config: [])
    process = MagicMock()
    process.create_time.return_value = 0
    process.memory_info.return_value.rss = 1024 * 1024
    monkeypatch.setattr("psutil.Process", MagicMock(return_value=process))

    result = CliRunner().invoke(cli, ["--config", str(bootstrap), command])

    assert result.exit_code == 0, result.output
    config.assert_called_once_with(apply_migrations=False, unknown_keys="skip")
    if command == "status":
        assert "Running (PID: 12345)" in result.output
        http_get.assert_not_called()
    else:
        assert "Gobby daemon: healthy (PID: 12345" in result.output
        http_get.assert_called_once_with("http://localhost:61234/api/health", timeout=2.0)


@pytest.mark.parametrize("command", ["status", "health"])
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(200, json={"status": "degraded", "degraded_services": ["qdrant"]}),
            "degraded",
        ),
        (httpx.Response(503), "unhealthy (HTTP 503)"),
        (httpx.Response(401), "authentication failed"),
        (httpx.Response(403), "unhealthy (HTTP 403)"),
        (
            httpx.Response(
                200,
                json={
                    "status": "degraded",
                    "hook_runtime": {"state": "schema_mismatch", "detail": "Reinstall ghook."},
                },
            ),
            "Reinstall ghook.",
        ),
        (httpx.Response(200, content=b"not JSON"), "invalid health response"),
        (httpx.Response(200, json=["ok"]), "invalid health response"),
        (httpx.Response(200, json={}), "invalid health response"),
        (httpx.Response(200, json={"status": "unknown"}), "invalid health response"),
        (httpx.Response(200, json={"status": ["ok"]}), "invalid health response"),
    ],
)
def test_managed_observability_does_not_report_false_health(
    command: str,
    response: httpx.Response,
    expected: str,
    bootstrap: Path,
    forbidden_config: tuple[MagicMock, MagicMock],
    http_get: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", str(bootstrap.parent / "grant.json"))
    http_get.return_value = response

    result = CliRunner().invoke(cli, ["--config", str(bootstrap), command])

    assert result.exit_code == 1
    assert expected in result.output
    assert "Gobby daemon: healthy" not in result.output
    for access in forbidden_config:
        access.assert_not_called()


@pytest.mark.parametrize("command", ["status", "health"])
@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("timeout")])
def test_managed_observability_reports_unreachable_daemon(
    command: str,
    error: httpx.RequestError,
    bootstrap: Path,
    forbidden_config: tuple[MagicMock, MagicMock],
    http_get: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", str(bootstrap.parent / "grant.json"))
    http_get.side_effect = error

    result = CliRunner().invoke(cli, ["--config", str(bootstrap), command])

    assert result.exit_code == 1
    assert "Gobby daemon: not responding" in result.output
    for access in forbidden_config:
        access.assert_not_called()
