"""Opt-in smoke test for serving packaged UI assets from an installed wheel."""

from __future__ import annotations

import logging
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
logger = logging.getLogger(__name__)


def _require_smoke_enabled() -> None:
    if os.environ.get("GOBBY_RUN_WHEEL_UI_SMOKE") != "1":
        pytest.skip("set GOBBY_RUN_WHEEL_UI_SMOKE=1 to run installed-wheel UI smoke")


def _resolve_wheel_path() -> Path:
    configured = os.environ.get("GOBBY_WHEEL_PATH")
    if configured:
        wheel = Path(configured)
    else:
        wheels = sorted(Path("dist").glob("gobby-*.whl"))
        if not wheels:
            pytest.fail("GOBBY_WHEEL_PATH was not set and dist/gobby-*.whl was not found")
        wheel = max(wheels, key=lambda path: path.stat().st_mtime)
    if not wheel.exists():
        pytest.fail(f"Wheel does not exist: {wheel}")
    return wheel.resolve()


def _venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _bind_high_port_socket(attempts: int = 100) -> socket.socket:
    last_error: OSError | None = None
    for _ in range(attempts):
        port = 49152 + secrets.randbelow(65535 - 49152)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            sock.listen(1)
        except OSError as exc:
            sock.close()
            last_error = exc
            logger.debug("Failed to bind candidate smoke-test port %s: %s", port, exc)
            continue
        return sock
    raise RuntimeError(
        f"could not allocate a high-numbered local port after {attempts} attempts; "
        f"last error: {last_error}"
    )


def _allocate_high_port() -> int:
    with _bind_high_port_socket() as sock:
        return sock.getsockname()[1]


def _allocate_distinct_high_ports() -> tuple[int, int]:
    attempts = 100
    for _ in range(attempts):
        with _bind_high_port_socket() as http_sock, _bind_high_port_socket() as ws_sock:
            http_port = http_sock.getsockname()[1]
            ws_port = ws_sock.getsockname()[1]
            if http_port != ws_port:
                return http_port, ws_port
    raise RuntimeError(
        f"could not allocate distinct HTTP and WebSocket smoke-test ports after {attempts} attempts"
    )


def _write_config(config_path: Path, db_path: Path, http_port: int, ws_port: int) -> None:
    config_path.write_text(
        "\n".join(
            [
                f'database_url: "{db_path}"',
                f"daemon_port: {http_port}",
                'bind_host: "127.0.0.1"',
                f"websocket_port: {ws_port}",
                "ui_port: 60889",
                "websocket:",
                f"  port: {ws_port}",
                "ui:",
                "  enabled: true",
                "  mode: production",
                "databases:",
                "  qdrant:",
                "    url: null",
                "  neo4j:",
                "    url: null",
                "embeddings:",
                "  api_base: null",
                "memory:",
                '  backend: "null"',
                "memory_sync:",
                "  enabled: false",
                "message_tracking:",
                "  enabled: false",
                "code_index:",
                "  enabled: false",
                "cron:",
                "  enabled: false",
                "",
            ]
        )
    )


def _wait_for_index(http_port: int, process: subprocess.Popen[object], log_path: Path) -> str:
    deadline = time.monotonic() + 60
    url = f"http://127.0.0.1:{http_port}/"
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(
                f"gobby.runner exited early with {process.returncode}\n{log_path.read_text()}"
            )
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:  # nosec B310
                body = response.read().decode("utf-8", errors="replace")
            if "<html" in body.lower():
                return body
            last_error = f"root response did not look like HTML: {body[:200]!r}"
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    pytest.fail(f"timed out waiting for {url}: {last_error}\n{log_path.read_text()}")


def test_installed_wheel_serves_packaged_index_html(tmp_path: Path) -> None:
    """Install a built wheel in an isolated venv and assert it serves packaged index.html."""
    _require_smoke_enabled()
    wheel = _resolve_wheel_path()

    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=120)
    python = _venv_python(venv)
    subprocess.run(
        [str(python), "-m", "pip", "install", str(wheel)],
        check=True,
        timeout=300,
    )

    home = tmp_path / "home"
    gobby_home = home / ".gobby"
    gobby_home.mkdir(parents=True)
    db_path = gobby_home / "hub-postgres.db"
    config_path = tmp_path / "config.yaml"
    http_port, ws_port = _allocate_distinct_high_ports()
    _write_config(config_path, db_path, http_port, ws_port)

    env = os.environ.copy()
    # The child daemon already has isolated HOME/GOBBY_HOME and an explicit
    # temp config. Letting it inherit GOBBY_TEST_PROTECT makes the temp DB look
    # like the protected production DB because Path.home() is also isolated.
    env.pop("GOBBY_TEST_PROTECT", None)
    env.pop("GOBBY_DATABASE_PATH", None)
    env.update(
        {
            "HOME": str(home),
            "GOBBY_HOME": str(gobby_home),
            "GOBBY_LOGGING_CLIENT": str(tmp_path / "client.log"),
            "GOBBY_LOGGING_CLIENT_ERROR": str(tmp_path / "client-error.log"),
            "GOBBY_LOGGING_MCP_SERVER": str(tmp_path / "mcp-server.log"),
            "GOBBY_LOGGING_MCP_CLIENT": str(tmp_path / "mcp-client.log"),
            "GOBBY_LOGGING_HOOK_MANAGER": str(tmp_path / "hook-manager.log"),
        }
    )

    log_path = tmp_path / "runner.log"
    with log_path.open("w") as log_file:
        process = subprocess.Popen(
            [str(python), "-m", "gobby.runner", "--config", str(config_path)],
            cwd=tmp_path,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            body = _wait_for_index(http_port, process, log_path)
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)

    assert "gobby" in body.lower()
