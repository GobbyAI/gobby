"""Exercise an enrolled native node against an isolated self-signed hub."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import ssl
import subprocess
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml
from websockets.sync.client import connect

from gobby.storage.api_keys import ApiKeyManager
from gobby.storage.hub.protocol import HubDatabase
from tests.e2e.conftest import (
    DaemonInstance,
    find_free_port,
    prepare_daemon_env,
    spawn_daemon_instance,
)
from tests.e2e.test_auth_login_e2e import TEST_EMAIL, _login
from tests.e2e.test_auth_login_e2e import e2e_pre_daemon_setup as e2e_pre_daemon_setup
from tests.fixtures.gdaemon_binary import select_test_gdaemon
from tests.fixtures.postgres import TEST_USER_ID
from tests.fixtures.tls_certs import pem_fingerprint

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(180)]
__all__ = ["e2e_pre_daemon_setup"]


@pytest.fixture
def pair_hub(
    e2e_project_dir: Path,
    e2e_config: tuple[Path, int, int],
    e2e_pre_daemon_setup: None,
) -> Generator[DaemonInstance]:
    """Seed the project and login before starting the hub front door."""
    bootstrap_path = e2e_config[0].parent / "bootstrap.yaml"
    bootstrap = yaml.safe_load(bootstrap_path.read_text(encoding="utf-8"))
    bootstrap["hub"] = True
    bootstrap_path.write_text(yaml.safe_dump(bootstrap), encoding="utf-8")
    yield from spawn_daemon_instance(e2e_project_dir, e2e_config, tls="self-signed")


@dataclass
class EnrolledNode:
    home: Path
    http_port: int
    websocket_port: int
    machine_id: str
    api_key: str
    key_id: str
    stderr_path: Path

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    def stderr(self) -> str:
        return self.stderr_path.read_text(encoding="utf-8")


@pytest.fixture
def pair_node(pair_hub: DaemonInstance, tmp_path: Path) -> Generator[EnrolledNode]:
    """Enroll and own a bare gdaemon process through every test exit path."""
    home = tmp_path / "node"
    home.mkdir()
    http_port = find_free_port()
    websocket_port = find_free_port()
    while websocket_port == http_port:
        websocket_port = find_free_port()
    bootstrap_path = home / "bootstrap.yaml"
    bootstrap_path.write_text(
        yaml.safe_dump(
            {
                "datastore_mode": "remote",
                "hub_daemon_url": f"https://127.0.0.1:{pair_hub.http_port}",
                "bind_host": "127.0.0.1",
                "daemon_port": http_port,
                "websocket_port": websocket_port,
            }
        ),
        encoding="utf-8",
    )
    bootstrap_path.chmod(0o600)
    assert pair_hub.cert_path is not None
    fingerprint = pem_fingerprint(pair_hub.cert_path.read_text(encoding="utf-8"))
    result = _login(home, "--fingerprint", fingerprint)
    assert result.returncode == 0, (TEST_EMAIL, result.stdout, result.stderr)
    bootstrap = yaml.safe_load(bootstrap_path.read_text(encoding="utf-8"))
    node = EnrolledNode(
        home=home,
        http_port=http_port,
        websocket_port=websocket_port,
        machine_id=(home / "machine_id").read_text(encoding="utf-8").strip(),
        api_key=bootstrap["api_key"],
        key_id=bootstrap["api_key_id"],
        stderr_path=home / "node.stderr",
    )
    repo_root = Path(__file__).resolve().parents[2]
    binary = select_test_gdaemon(repo_root, os.environ, "gdaemon")
    assert binary is not None, "Run with GOBBY_TEST_GDAEMON=checkout after cargo build"
    env = prepare_daemon_env(home_dir=home)
    env["GOBBY_HOME"] = str(home)
    env.pop("GOBBY_CONFIG", None)
    env.pop("DATABASE_URL", None)
    with node.stderr_path.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            [str(binary), "serve"],
            cwd=home,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
        try:
            yield node
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)


def _wait_listener(client: httpx.Client, node: EnrolledNode) -> None:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            response = client.get("/api/health")
            if response.status_code == 200 and time.monotonic() < deadline:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.25)
    pytest.fail(f"Node listener was not ready within 30s.\n{node.stderr()}")


def _wait_connection(
    observer: httpx.Client, node: EnrolledNode, *, connected: bool, deadline: float
) -> None:
    """Credit only a successful observation received before the deadline."""
    last_response = "No response"
    while time.monotonic() < deadline:
        try:
            response = observer.get(f"/api/machines/{node.machine_id}")
            received_at = time.monotonic()
            last_response = f"{response.status_code}: {response.text}"
            if (
                received_at < deadline
                and response.status_code == 200
                and response.json()["connected"] is connected
            ):
                return
        except httpx.TransportError as exc:
            last_response = str(exc)
        time.sleep(0.25)
    pytest.fail(
        f"Node connected={connected} was not observed before deadline. "
        f"Last response: {last_response}\n{node.stderr()}"
    )


def test_node_enrolls_and_relays(
    pair_hub: DaemonInstance, pair_node: EnrolledNode, postgres_db: HubDatabase
) -> None:
    """1.6.1: TLS enrollment, HTTP/WS relay, bound grant, and bounded revocation."""
    assert pair_hub.cert_path is not None
    pinned_tls = ssl.create_default_context(cafile=pair_hub.cert_path)
    keys = ApiKeyManager(postgres_db)
    observer_key, _ = keys.mint(
        TEST_USER_ID, "21000000-0000-4000-8000-000000000002", "pair-observer"
    )
    with (
        httpx.Client(
            base_url=f"https://127.0.0.1:{pair_hub.http_port}",
            verify=pinned_tls,
            headers={"Authorization": f"Bearer {observer_key}"},
            timeout=1.0,
        ) as observer,
        httpx.Client(base_url=pair_node.origin, timeout=1.0) as client,
    ):
        _wait_listener(client, pair_node)
        _wait_connection(observer, pair_node, connected=True, deadline=time.monotonic() + 30.0)
        authorization = {"Authorization": f"Bearer {pair_node.api_key}"}
        health = client.get("/api/health", headers=authorization)
        assert health.status_code == 200, health.text
        assert health.json()["mode"] == "hub"
        machines = client.get("/api/machines", headers=authorization)
        assert machines.status_code == 200, machines.text
        enrolled = next(
            machine
            for machine in machines.json()["machines"]
            if machine["id"] == pair_node.machine_id
        )
        assert enrolled["connected"] is True

        with connect(
            f"ws://127.0.0.1:{pair_node.websocket_port}",
            additional_headers=authorization,
            open_timeout=5.0,
            close_timeout=5.0,
        ) as websocket:
            welcome = websocket.recv(timeout=5.0)
            assert json.loads(welcome)["type"] == "connection_established"
            websocket.send(json.dumps({"type": "ping"}))
            message = websocket.recv(timeout=5.0)
            assert json.loads(message)["type"] == "pong"

        nonce = os.urandom(32)
        challenge = client.post(
            "/api/runtime/handshake/challenge",
            json={
                "nonce": base64.urlsafe_b64encode(nonce).decode().rstrip("="),
                "kind": "interactive",
            },
        )
        assert challenge.status_code == 200, challenge.text
        proof_key = hmac.digest(
            pair_node.api_key.encode(), b"gobby-interactive-proof-v1", hashlib.sha256
        )
        assert challenge.json()["proof"] == hmac.new(proof_key, nonce, hashlib.sha256).hexdigest()
        handshake = client.post(
            "/api/runtime/handshake",
            headers=authorization,
            json={
                "machine_id": pair_node.machine_id,
                "project_id": "00000000-0000-0000-0000-000000000e2e",
                "session_id": None,
            },
        )
        assert handshake.status_code == 200, handshake.text
        assert handshake.json()["grant"]["principal"]["machine_id"] == pair_node.machine_id

        assert keys.revoke(pair_node.key_id, TEST_USER_ID)
        revoked_at = time.monotonic()
        _wait_connection(observer, pair_node, connected=False, deadline=revoked_at + 30.0)
        refused = client.get("/api/machines", headers=authorization)
        assert refused.status_code == 401, refused.text
