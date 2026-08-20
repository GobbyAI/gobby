"""Process-level acceptance coverage for the shared PostgreSQL daemon lease."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import (
    DaemonInstance,
    _postgres_url_for_schema,
    _seed_e2e_runtime_state,
    find_free_port,
    prepare_daemon_env,
    terminate_process_tree,
    wait_for_daemon_health,
    wait_for_port,
)

pytestmark = pytest.mark.e2e


def _write_daemon_home(
    root: Path,
    *,
    database_url: str,
    http_port: int,
    ws_port: int,
    machine_id: str,
) -> Path:
    home = root
    log_dir = home / "logs"
    log_dir.mkdir(parents=True)
    (home / "machine_id").write_text(machine_id)
    config_path = home / "config.yaml"
    config_path.write_text(
        f"""daemon_port: {http_port}
test_mode: true
database_url: "{home / "hub-postgres.db"}"

websocket:
  enabled: true
  port: {ws_port}

logging:
  client: "{log_dir / "client.log"}"
  client_error: "{log_dir / "client_error.log"}"

gobby_tasks:
  expansion:
    enabled: false
  validation:
    enabled: false

code_index:
  enabled: false

memory:
  dream:
    enabled: false
"""
    )
    bootstrap_path = home / "bootstrap.yaml"
    files_home = home / "files"
    files_home.mkdir(exist_ok=True)
    bootstrap_path.write_text(
        f"""hub_backend: postgres
database_url: {database_url}
daemon_port: {http_port}
bind_host: localhost
websocket_port: {ws_port}
files_home: {files_home}
"""
    )
    bootstrap_path.chmod(0o600)
    return config_path


def _spawn_daemon(
    project_dir: Path, config_path: Path, http_port: int, ws_port: int
) -> DaemonInstance:
    home = config_path.parent
    log_file = home / "logs" / "daemon.log"
    error_log_file = home / "logs" / "daemon_error.log"
    env = prepare_daemon_env(home_dir=home)
    env["GOBBY_CONFIG"] = str(config_path)
    env["GOBBY_HOME"] = str(home)
    target_debug = Path(__file__).parents[2] / "target" / "debug"
    env["PATH"] = f"{target_debug}:{env.get('PATH', '')}"
    command = [sys.executable, "-m", "gobby.runner", "--config", str(config_path)]
    with log_file.open("wb") as log_handle, error_log_file.open("wb") as error_handle:
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=error_handle,
            stdin=subprocess.DEVNULL,
            cwd=project_dir,
            env=env,
            start_new_session=True,
        )
    return DaemonInstance(
        process=process,
        pid=process.pid,
        http_port=http_port,
        ws_port=ws_port,
        project_dir=project_dir,
        gobby_dir=project_dir / ".gobby",
        log_file=log_file,
        error_log_file=error_log_file,
        db_path=home / "hub-postgres.db",
        config_path=config_path,
        command=command,
        env=env,
    )


def _wait_for_standby(instance: DaemonInstance, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if instance.process.poll() is not None:
            pytest.fail(
                f"Standby exited with {instance.process.returncode}.\n"
                f"Logs:\n{instance.read_logs()}\nErrors:\n{instance.read_error_logs()}"
            )
        try:
            response = httpx.get(f"{instance.http_url}/api/health", timeout=1.0)
            if response.status_code == 200 and response.json().get("lease_mode") == "standby":
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    pytest.fail(
        f"Standby control endpoint did not become ready.\n"
        f"Logs:\n{instance.read_logs()}\nErrors:\n{instance.read_error_logs()}"
    )


def _stop(instance: DaemonInstance) -> None:
    if instance.is_alive():
        terminate_process_tree(instance.pid)


def test_single_active_daemon_and_explicit_handoff(
    e2e_project_dir: Path,
    postgres_database_url: str,
    postgres_schema: str,
    postgres_db: object,
) -> None:
    """One full runtime owns the lease until an explicit, quiescent handoff."""
    _seed_e2e_runtime_state(postgres_db, e2e_project_dir)
    ports: list[int] = []
    while len(ports) < 4:
        port = find_free_port()
        if port not in ports:
            ports.append(port)
    active_machine_id = str(uuid.uuid4())
    standby_machine_id = str(uuid.uuid4())
    scoped_database_url = _postgres_url_for_schema(postgres_database_url, postgres_schema)
    active_config = _write_daemon_home(
        e2e_project_dir / "active-home",
        database_url=scoped_database_url,
        http_port=ports[0],
        ws_port=ports[1],
        machine_id=active_machine_id,
    )
    standby_config = _write_daemon_home(
        e2e_project_dir / "standby-home",
        database_url=scoped_database_url,
        http_port=ports[2],
        ws_port=ports[3],
        machine_id=standby_machine_id,
    )
    active = _spawn_daemon(e2e_project_dir, active_config, ports[0], ports[1])
    standby: DaemonInstance | None = None
    try:
        assert wait_for_daemon_health(active.http_port, timeout=30.0), (
            f"Active daemon failed startup.\nLogs:\n{active.read_logs()}\n"
            f"Errors:\n{active.read_error_logs()}"
        )
        assert wait_for_port(active.ws_port, timeout=10.0)

        token_path = active.gobby_home / "local_cli_token"
        assert token_path.exists()
        for credential_name in ("local_cli_token", ".secret_kek"):
            source = active.gobby_home / credential_name
            if source.exists():
                shutil.copy2(source, standby_config.parent / credential_name)
        token = token_path.read_text().strip()
        headers = {"Authorization": f"Bearer {token}"}

        standby = _spawn_daemon(e2e_project_dir, standby_config, ports[2], ports[3])
        _wait_for_standby(standby)
        assert active.gobby_home != standby.gobby_home
        assert active_machine_id != standby_machine_id
        assert httpx.get(f"{standby.http_url}/mcp", timeout=2.0).status_code == 404
        assert httpx.get(f"{standby.http_url}/api/sessions", timeout=2.0).status_code == 404
        assert not wait_for_port(standby.ws_port, timeout=1.0)
        assert "gdaemon schema apply completed" not in standby.read_logs()

        held = httpx.post(
            f"{standby.http_url}/api/admin/lease/promote",
            headers=headers,
            timeout=5.0,
        )
        assert held.status_code == 409

        handoff = httpx.post(
            f"{active.http_url}/api/admin/lease/handoff",
            headers=headers,
            timeout=5.0,
        )
        assert handoff.status_code == 200, handoff.text
        active.process.wait(timeout=15.0)

        promoted = httpx.post(
            f"{standby.http_url}/api/admin/lease/promote",
            headers=headers,
            timeout=5.0,
        )
        assert promoted.status_code == 200, promoted.text
        assert wait_for_daemon_health(standby.http_port, timeout=30.0), (
            f"Promoted daemon failed startup.\nLogs:\n{standby.read_logs()}\n"
            f"Errors:\n{standby.read_error_logs()}"
        )
        assert wait_for_port(standby.ws_port, timeout=10.0)
        lease_status = httpx.get(
            f"{standby.http_url}/api/admin/lease/status",
            headers=headers,
            timeout=5.0,
        )
        assert lease_status.status_code == 200
        assert lease_status.json()["mode"] == "active"
        assert standby_machine_id in lease_status.json()["owner_application_name"]
    finally:
        if standby is not None:
            _stop(standby)
        _stop(active)
