"""Isolated Python daemon for the v3 recorder, started the way the e2e fixtures start one.

The launch route is tests/e2e/conftest.py's: the same gdaemon front door over the Python
runner, the same readiness and cleanup helpers, and the same environment. This module only
replaces the pytest fixtures that wrap them (``postgres_db``, ``e2e_config``) with plain
context managers, so the recorder runs without pytest. Teardown follows this order:
the daemon tree stops first, then the caller drops the per-run database.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.storage.api_keys import ensure_local_api_key
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.utils.dev import WORKTREE_DAEMON_OVERRIDE_ENV
from tests.contracts.recorder_v3_support import ProviderEdgeLog
from tests.e2e import conftest as e2e
from tests.fixtures.gdaemon_binary import build_checkout_gdaemon
from tests.fixtures.hub_guard import guard_worktree_override
from tests.fixtures.postgres import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    TEST_USER_NAME,
    TEST_USER_PASSWORD_HASH,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_MACHINE_ID = "21000000-0000-4000-8000-000000000002"
SCHEMA_APPLY_TIMEOUT_SECONDS = 300


class RecorderDaemonError(RuntimeError):
    """The isolated daemon could not be prepared or started."""


@dataclass(frozen=True)
class RecorderDaemon:
    """A running isolated daemon: its URLs, its home and the operator bearer."""

    http_url: str
    ws_port: int
    home: Path
    project_dir: Path
    pid: int

    def operator_headers(self) -> dict[str, str]:
        """Bearer headers for the bootstrap key, read from the isolated home."""
        return e2e.daemon_auth_headers(self.home)

    def operator_token(self) -> str:
        return e2e.daemon_token(self.home)


def _backend_port_is_free(port: int) -> bool:
    """The front door proxies to its runner at port + 100, so that backend port must be free."""
    if port + 100 > 65535:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("0.0.0.0", port + 100))
        except OSError:
            return False
    return True


def _free_port_pair() -> tuple[int, int]:
    """Return two distinct front-door ports, each with its backend port free (as e2e_config)."""
    ports: list[int] = []
    while len(ports) < 2:
        port = e2e.find_free_port()
        if any(abs(port - taken) < 100 for taken in ports) or not _backend_port_is_free(port):
            continue
        ports.append(port)
    return ports[0], ports[1]


def _write_home(home: Path, *, database_url: str, http_port: int, ws_port: int) -> Path:
    """Write the isolated home the e2e config fixture writes; return the config path."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "machine_id").write_text(E2E_MACHINE_ID)
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    files_home = home / "files"
    files_home.mkdir(exist_ok=True)
    config_path = home / "config.yaml"
    config_path.write_text(
        f"""
daemon_port: {http_port}
test_mode: true
database_url: "{home / "hub-postgres.db"}"

websocket:
  enabled: true
  port: {ws_port}
  ping_interval: 30
  ping_timeout: 10

logging:
  client: "{log_dir}/client.log"
  client_error: "{log_dir}/client_error.log"

session_lifecycle:
  idle_timeout_minutes: 60
  max_sessions_per_machine: 10
  cleanup_interval_minutes: 5

gobby-tasks:
  expansion:
    enabled: false
  validation:
    enabled: false

code_index:
  enabled: false

embeddings:
  api_base: "http://127.0.0.1:11434/v1"

memory:
  dream:
    enabled: false

session_summary:
  summary_file_path: "{home}/session_summaries"
"""
    )
    bootstrap_path = home / "bootstrap.yaml"
    bootstrap_path.write_text(
        f"""
hub_backend: postgres
database_url: {database_url}
daemon_port: {http_port}
bind_host: localhost
websocket_port: {ws_port}
files_home: {files_home}
front_door:
  enabled: true
"""
    )
    bootstrap_path.chmod(0o600)
    return config_path


def _apply_schema(database_url: str, home: Path) -> None:
    """Apply the checkout's gdaemon schema to the per-run database, under the isolated home."""
    gdaemon = build_checkout_gdaemon(REPO_ROOT, os.environ, "gdaemon")
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "GOBBY_DATABASE_URL": database_url,
    }
    result = subprocess.run(
        [str(gdaemon), "schema", "apply"],
        env=env,
        capture_output=True,
        text=True,
        timeout=SCHEMA_APPLY_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RecorderDaemonError(f"gdaemon schema apply failed: {result.stderr.strip()}")


def _seed_rows(database: PostgresHubDatabase) -> None:
    """Insert the user and the machine the isolated daemon registers as, as the hub seed does."""
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO users (id, email, name, password_hash)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (TEST_USER_ID, TEST_USER_EMAIL, TEST_USER_NAME, TEST_USER_PASSWORD_HASH),
        )
        connection.execute(
            """
            INSERT INTO machines (id, owner_user_id)
            VALUES (%s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (E2E_MACHINE_ID, TEST_USER_ID),
        )


@contextmanager
def _worktree_override(base_url: str) -> Iterator[None]:
    """Allow the linked-worktree daemon for this run only, when the hub guard accepts the base.

    The daemon refuses a linked worktree unless GOBBY_ALLOW_WORKTREE_DAEMON is set. The guard
    sets it only for an accepted base URL, and the previous value is restored on exit.
    """
    previous = os.environ.get(WORKTREE_DAEMON_OVERRIDE_ENV)
    if not guard_worktree_override(os.environ, base_url, None):
        raise RecorderDaemonError("the hub guard refused the worktree daemon override")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(WORKTREE_DAEMON_OVERRIDE_ENV, None)
        else:
            os.environ[WORKTREE_DAEMON_OVERRIDE_ENV] = previous


@contextmanager
def recorder_daemon(
    *,
    base_url: str,
    database_url: str,
    startup: Mapping[str, Any] | None = None,
    edge: ProviderEdgeLog | None = None,
) -> Iterator[RecorderDaemon]:
    """Start one isolated daemon on ``database_url`` and stop it on exit.

    ``base_url`` is the guarded DATABASE_URL the run derives from; ``database_url`` is the run's
    own database. The caller owns the database and drops it after this context exits. ``startup``
    (a family's startup block) and ``edge`` (a provider edge) are accepted here but not yet applied.
    """
    with _daemon(database_url, base_url) as daemon:
        yield daemon


@contextmanager
def _daemon(database_url: str, base_url: str) -> Iterator[RecorderDaemon]:
    root = Path(tempfile.mkdtemp(prefix="gobby_v3_")).resolve()
    socket_dir = e2e.create_host_socket_dir()
    gen: Any = None
    database: PostgresHubDatabase | None = None
    try:
        e2e.build_e2e_project(root)
        home = root / ".gobby-home"
        http_port, ws_port = _free_port_pair()
        config_path = _write_home(
            home, database_url=database_url, http_port=http_port, ws_port=ws_port
        )
        _apply_schema(database_url, home)
        database = PostgresHubDatabase(database_url)
        _seed_rows(database)
        e2e._seed_e2e_runtime_state(database, root, terminal_host_socket_dir=socket_dir)
        ensure_local_api_key(database, E2E_MACHINE_ID, home / "bootstrap.yaml")
        with _worktree_override(base_url):
            gen = e2e._start_daemon_instance(root, (config_path, http_port, ws_port))
            instance = next(gen)
        yield RecorderDaemon(
            http_url=instance.http_url,
            ws_port=instance.ws_port,
            home=home,
            project_dir=root,
            pid=instance.pid,
        )
    finally:
        if gen is not None:
            next(gen, None)
        if database is not None:
            database.close()
        e2e.stop_terminal_host(socket_dir)
        shutil.rmtree(socket_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
