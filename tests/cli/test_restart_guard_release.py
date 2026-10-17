"""A replacement runner may fence the CLI's read-only shutdown connection."""

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import psycopg
import pytest
from click.testing import CliRunner

from gobby.cli import daemon_lifecycle as lifecycle
from gobby.cli.runtime import CliRuntime
from gobby.config.app import DaemonConfig
from gobby.runner_gate import _run_gate_request, _terminal_fence_key
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.utils.machine_id import require_machine_id

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("ready", [True, False], ids=["healthy", "startup-failed"])
def test_restart_guard_survives_real_runner_gate_fencing(
    monkeypatch: pytest.MonkeyPatch, postgres_db: PostgresHubDatabase, ready: bool
) -> None:
    token = uuid4().hex
    monkeypatch.setattr("gobby.storage.hub.postgres.deployment_token", lambda: token)
    db = PostgresHubDatabase(postgres_db.conninfo)
    predecessor_db = PostgresHubDatabase(postgres_db.conninfo, lifecycle="runner")
    runtime = MagicMock(spec=CliRuntime)
    runtime.require_database.return_value = db
    runtime.read_only_operational_config.return_value = DaemonConfig(daemon_port=61987)
    monkeypatch.setattr("gobby.cli.runtime.get_cli_runtime", lambda _ctx: runtime)
    monkeypatch.setattr(lifecycle, "is_test_protect_enabled", lambda: False)
    monkeypatch.setattr(lifecycle, "restart_start_refusal", lambda _ctx: None)
    monkeypatch.setattr(lifecycle, "_is_daemon_healthy", lambda _port: True)
    monkeypatch.setattr(lifecycle, "clear_protected_runs", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(lifecycle, "daemon_auth_headers", lambda: {})
    monkeypatch.setattr(lifecycle, "_is_process_alive", lambda _pid: False)

    def readiness(_port: int, *, max_wait: float) -> bool:
        with psycopg.connect(db.conninfo, autocommit=True) as conn:
            with conn.transaction():
                acquired = conn.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"gobby:handoff-shutdown:{require_machine_id()}",),
                ).fetchone()
                assert acquired == (True,), "Shutdown guard must end before readiness polling"
        return ready

    poll = MagicMock(side_effect=readiness)
    monkeypatch.setattr("gobby.cli.daemon_start._poll_startup_progress", poll)

    def restart_request(*_args: Any, **_kwargs: Any) -> httpx.Response:
        # This is the new runner's actual gate, scoped to this test's deployment.
        # The CLI transaction must still hold its shutdown lock at acceptance.
        with psycopg.connect(db.conninfo, autocommit=True) as conn:
            row = conn.execute(
                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
                "WHERE a.application_name = %s AND l.locktype = 'advisory' AND l.granted",
                (db.application_name,),
            ).fetchone()
            assert row is not None and row[0] == 1
        with psycopg.connect(
            db.conninfo, autocommit=True, application_name=predecessor_db.application_name
        ) as predecessor:
            assert (
                _run_gate_request(
                    {
                        "conninfo": db.conninfo,
                        "deployment_token": token,
                        "fence_key": _terminal_fence_key(token),
                        "successor_application_name": f"gobby-hub-{token}-successor",
                    }
                )["success"]
                is True
            )
            with pytest.raises(psycopg.errors.AdminShutdown):
                predecessor.execute("SELECT 1")
        return httpx.Response(200, json={"status": "restarting", "backend_pid": 42})

    monkeypatch.setattr("gobby.cli.daemon_lifecycle.httpx.post", restart_request)
    try:
        result = CliRunner().invoke(lifecycle.restart, ["--wait"])
        # The real guard committed, and its independent pool remains usable.
        assert db.fetchone("SELECT 1 AS alive") == {"alive": 1}
    finally:
        db.close()
        predecessor_db.close()

    assert result.exit_code == (0 if ready else 1), (result.output, result.exception)
    if ready:
        assert result.exception is None
    else:
        assert isinstance(result.exception, SystemExit)
    poll.assert_called_once()
    assert poll.call_args.args == (61987,)
