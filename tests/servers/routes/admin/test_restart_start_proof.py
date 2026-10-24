"""POST /api/admin/restart proves the start half before it stops the daemon."""

import asyncio
import contextlib
import os
import signal
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from gobby.servers.http import HTTPServer
from gobby.servers.routes.admin import _lifecycle
from gobby.servers.routes.admin._lifecycle import register_lifecycle_routes
from gobby.storage.schema_contract import SchemaContractError, plan_schema

pytestmark = pytest.mark.unit

REFUSAL = "Refusing to publish dirty bundled content (modified: shared/skills/demo/SKILL.md)."


class ProofStarted(asyncio.DatagramProtocol):
    def __init__(self, started: asyncio.Future[int]) -> None:
        self.started = started

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if not self.started.done():
            self.started.set_result(int(data))


@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
async def test_restart_probe_reaps_slow_gdaemon_before_finishing(
    interruption: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop_boundaries: dict[str, Any],
) -> None:
    loop = asyncio.get_running_loop()
    started: asyncio.Future[int] = loop.create_future()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: ProofStarted(started), local_addr=("127.0.0.1", 0)
    )
    port = transport.get_extra_info("sockname")[1]
    binary = tmp_path / "gdaemon"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os, socket, threading\n"
        "with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as ready:\n"
        f"    ready.sendto(str(os.getpid()).encode(), ('127.0.0.1', {port}))\n"
        "threading.Event().wait(30)\n"
    )
    binary.chmod(0o700)
    finished = threading.Event()

    def proof() -> str | None:
        try:
            plan_schema(
                "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test", gdaemon=binary
            )
        except SchemaContractError as exc:
            return str(exc)
        finally:
            finished.set()
        return None

    monkeypatch.setattr(_lifecycle, "_restart_start_refusal", proof)
    monkeypatch.setattr(_lifecycle, "RESTART_START_PROOF_TIMEOUT_SECONDS", 0.5)
    app = _client().app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        request = asyncio.create_task(client.post("/api/admin/restart"))
        pid: int | None = None
        try:
            pid = await asyncio.wait_for(asyncio.shield(started), timeout=5)
            if interruption == "cancel":
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
            else:
                response = await request
                assert response.status_code == 409
                assert response.json()["status"] == "start_refused"
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            assert finished.is_set()
            stop_boundaries["prepare"].assert_not_called()
            stop_boundaries["write"].assert_not_called()
            stop_boundaries["request"].assert_not_called()
            assert _lifecycle._restart_lock is not None
            assert not _lifecycle._restart_lock.locked()
        finally:
            transport.close()
            if pid is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            if not request.done():
                request.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await request
            assert await asyncio.to_thread(finished.wait, 5)


class OperatorAuth:
    """An operator caller: no managed principal, so the release window fence passes."""

    def request_principal(self, _request: object) -> None:
        return None


class FakeServer:
    auth_service = OperatorAuth()

    def get_runner(self) -> None:
        return None

    async def run_db(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)


@pytest.fixture
def stop_boundaries(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(_lifecycle, "_restart_lock", None)
    monkeypatch.setattr(_lifecycle, "require_machine_id", lambda: "machine")
    prepare = AsyncMock()
    monkeypatch.setattr(_lifecycle, "_prepare_handoff_shutdown", prepare)
    request = MagicMock(return_value=True)
    monkeypatch.setattr(_lifecycle, "_request_runner_shutdown", request)
    write = MagicMock()
    monkeypatch.setattr("gobby.runner_maintenance.write_shutdown_source", write)
    return {"prepare": prepare, "request": request, "write": write}


def _client() -> TestClient:
    router = APIRouter(prefix="/api/admin")
    register_lifecycle_routes(router, cast(HTTPServer, FakeServer()))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_restart_refuses_before_stopping_when_the_start_proof_fails(
    monkeypatch: pytest.MonkeyPatch, stop_boundaries: dict[str, Any]
) -> None:
    monkeypatch.setattr("gobby.cli.daemon_preflight.restart_start_refusal", lambda _rt: REFUSAL)

    response = _client().post("/api/admin/restart")

    assert response.status_code == 409
    assert response.json() == {"status": "start_refused", "message": REFUSAL}
    stop_boundaries["prepare"].assert_not_called()
    stop_boundaries["write"].assert_not_called()
    stop_boundaries["request"].assert_not_called()
    assert _lifecycle._restart_lock is not None
    assert not _lifecycle._restart_lock.locked()


def test_force_does_not_bypass_the_start_proof(
    monkeypatch: pytest.MonkeyPatch, stop_boundaries: dict[str, Any]
) -> None:
    monkeypatch.setattr("gobby.cli.daemon_preflight.restart_start_refusal", lambda _rt: REFUSAL)

    response = _client().post("/api/admin/restart?force=true")

    assert response.status_code == 409
    stop_boundaries["write"].assert_not_called()
    stop_boundaries["request"].assert_not_called()


def test_restart_proceeds_when_the_start_proof_passes(
    monkeypatch: pytest.MonkeyPatch, stop_boundaries: dict[str, Any]
) -> None:
    monkeypatch.setattr("gobby.cli.daemon_preflight.restart_start_refusal", lambda _rt: None)

    response = _client().post("/api/admin/restart")

    assert response.status_code == 200
    assert response.json()["status"] == "restarting"
    stop_boundaries["request"].assert_called_once()


def test_windows_restart_refuses_before_spawning_the_helper(
    monkeypatch: pytest.MonkeyPatch, stop_boundaries: dict[str, Any]
) -> None:
    monkeypatch.setattr("gobby.cli.daemon_preflight.restart_start_refusal", lambda _rt: REFUSAL)
    spawn = MagicMock()
    monkeypatch.setattr("gobby.runner_windows_restart.spawn_restart_helper", spawn)
    monkeypatch.setattr(_lifecycle, "os", SimpleNamespace(name="nt", getpid=os.getpid))

    response = _client().post("/api/admin/restart")

    assert response.status_code == 409
    assert response.json() == {"status": "start_refused", "message": REFUSAL}
    spawn.assert_not_called()
    stop_boundaries["prepare"].assert_not_called()
    stop_boundaries["write"].assert_not_called()
    stop_boundaries["request"].assert_not_called()


def test_slow_start_proof_refuses_before_stopping(
    monkeypatch: pytest.MonkeyPatch, stop_boundaries: dict[str, Any]
) -> None:
    release = threading.Event()

    def slow_proof(_runtime: object) -> None:
        release.wait(5)

    monkeypatch.setattr("gobby.cli.daemon_preflight.restart_start_refusal", slow_proof)
    monkeypatch.setattr(_lifecycle, "RESTART_START_PROOF_TIMEOUT_SECONDS", 0.05, raising=False)

    try:
        response = _client().post("/api/admin/restart")
    finally:
        release.set()

    assert response.status_code == 409
    assert response.json()["status"] == "start_refused"
    assert "did not finish" in response.json()["message"]
    stop_boundaries["prepare"].assert_not_called()
    stop_boundaries["write"].assert_not_called()
    stop_boundaries["request"].assert_not_called()
    assert _lifecycle._restart_lock is not None
    assert not _lifecycle._restart_lock.locked()


def test_slow_start_proof_timeout_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    stop_boundaries: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = threading.Event()

    def slow_proof(_runtime: object) -> None:
        release.wait(5)

    monkeypatch.setattr("gobby.cli.daemon_preflight.restart_start_refusal", slow_proof)
    monkeypatch.setattr(_lifecycle, "RESTART_START_PROOF_TIMEOUT_SECONDS", 0.05, raising=False)
    caplog.set_level("WARNING", logger=_lifecycle.__name__)

    try:
        _client().post("/api/admin/restart")
    finally:
        release.set()

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "start proof timed out" in warnings[0]
    assert "refused before stop" in warnings[0]
