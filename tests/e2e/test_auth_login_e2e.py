"""Live-hub end-to-end coverage for `gobby auth login` certificate pinning."""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.identity import hash_password
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.users import LocalUserManager
from gobby.utils import api_key_format
from tests.e2e.conftest import DaemonInstance, prepare_daemon_env, spawn_daemon_instance
from tests.fixtures.postgres import TEST_USER_ID
from tests.fixtures.tls_certs import pem_fingerprint, write_self_signed_pair

pytestmark = pytest.mark.e2e

TEST_EMAIL = "login-e2e-user@gobby.local"
TEST_PASSWORD = "login-e2e-password"
PRIOR_KEY_ID = "1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b"


@pytest.fixture
def e2e_pre_daemon_setup(postgres_db: HubDatabase) -> None:
    """Seed the canonical user's login before the isolated hub starts."""
    users = LocalUserManager(postgres_db)
    users.update_profile(TEST_USER_ID, name="Login E2E User", email=TEST_EMAIL)
    users.update_password(TEST_USER_ID, hash_password(TEST_PASSWORD))


@pytest.fixture
def self_signed_hub(
    e2e_project_dir: Path, e2e_config: tuple[Path, int, int], e2e_pre_daemon_setup: None
) -> Generator[DaemonInstance]:
    """The isolated hub with gdaemon's generated certificate on its front door."""
    _ = e2e_pre_daemon_setup
    yield from spawn_daemon_instance(e2e_project_dir, e2e_config, tls="self-signed")


@pytest.fixture
def narrow_san_hub(
    e2e_project_dir: Path, e2e_config: tuple[Path, int, int], e2e_pre_daemon_setup: None
) -> Generator[DaemonInstance]:
    """A `files`-mode hub whose certificate names only `hub.invalid`."""
    _ = e2e_pre_daemon_setup
    write_self_signed_pair(e2e_config[0].parent / "tls", ["hub.invalid"], stem="front_door")
    yield from spawn_daemon_instance(e2e_project_dir, e2e_config, tls="files")


def _node_home(root: Path, origin: str, *, prior: Path | None = None) -> Path:
    home = root / "node"
    home.mkdir(parents=True)
    data: dict[str, Any] = {"datastore_mode": "remote", "hub_daemon_url": origin}
    if prior is not None:
        pem = home / "tls" / "hub.pem"
        pem.parent.mkdir(mode=0o700)
        pem.write_text(prior.read_text(encoding="utf-8"), encoding="utf-8")
        pem.chmod(0o600)
        data.update(api_key=api_key_format.generate(), api_key_id=PRIOR_KEY_ID, hub_cert=str(pem))
    bootstrap = home / "bootstrap.yaml"
    bootstrap.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    bootstrap.chmod(0o600)
    return home


def _enrollment_files(home: Path) -> dict[str, bytes]:
    files = [home / "bootstrap.yaml", *sorted((home / "tls").glob("*"))]
    return {str(path.relative_to(home)): path.read_bytes() for path in files if path.is_file()}


def _login(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = prepare_daemon_env(home_dir=home)
    env["GOBBY_HOME"] = str(home)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from gobby.cli import cli; cli()",
            "auth",
            "login",
            "--email",
            TEST_EMAIL,
            *args,
        ],
        cwd=home,
        env=env,
        input=f"{TEST_PASSWORD}\n",
        capture_output=True,
        text=True,
        timeout=60.0,
        check=False,
    )


def _live_key_ids(db: HubDatabase) -> set[str]:
    rows = db.fetchall("SELECT id::TEXT AS id FROM api_keys WHERE revoked_at IS NULL")
    return {row["id"] for row in rows}


@contextmanager
def _ipv6_relay(target_port: int) -> Iterator[int]:
    """Relay `[::1]:<port>` to the hub's IPv4 loopback listener, TLS left end to end."""
    listener = socket.create_server(("::1", 0), family=socket.AF_INET6)
    listener.settimeout(0.1)
    stop = threading.Event()
    open_sockets: list[socket.socket] = []

    def _pump(source: socket.socket, sink: socket.socket) -> None:
        try:
            while chunk := source.recv(65536):
                sink.sendall(chunk)
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            return

    def _accept() -> None:
        while not stop.is_set():
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            client.settimeout(None)
            upstream = socket.create_connection(("127.0.0.1", target_port))
            open_sockets.extend((client, upstream))
            for source, sink in ((client, upstream), (upstream, client)):
                threading.Thread(target=_pump, args=(source, sink), daemon=True).start()

    acceptor = threading.Thread(target=_accept, daemon=True)
    acceptor.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        acceptor.join(timeout=5)
        listener.close()
        for open_socket in open_sockets:
            open_socket.close()


def test_login_pins_self_signed_hub(
    self_signed_hub: DaemonInstance, postgres_db: HubDatabase, tmp_path: Path
) -> None:
    """4.5.1: login pins the hub's self-signed leaf over IPv4 and IPv6 loopback."""
    assert self_signed_hub.cert_path is not None
    served_pem = self_signed_hub.cert_path.read_text(encoding="utf-8")
    fingerprint = pem_fingerprint(served_pem)
    port = self_signed_hub.http_port
    hub_keys = _live_key_ids(postgres_db)

    v4_home = _node_home(tmp_path / "v4", f"https://127.0.0.1:{port}")
    before = _enrollment_files(v4_home)
    mismatch = _login(v4_home, "--fingerprint", f"sha256:{'0' * 64}")
    assert mismatch.returncode != 0
    assert fingerprint in mismatch.stdout + mismatch.stderr
    assert _enrollment_files(v4_home) == before
    assert _live_key_ids(postgres_db) == hub_keys

    with _ipv6_relay(port) as v6_port:
        v6_home = _node_home(tmp_path / "v6", f"https://[::1]:{v6_port}")
        enrolled = {
            "127.0.0.1": (v4_home, _login(v4_home, "--fingerprint", fingerprint)),
            "::1": (v6_home, _login(v6_home, "--fingerprint", fingerprint)),
        }

    minted: set[str] = set()
    for host, (home, result) in enrolled.items():
        assert result.returncode == 0, (host, result.stdout, result.stderr)
        assert fingerprint in result.stdout, host
        data = yaml.safe_load((home / "bootstrap.yaml").read_text(encoding="utf-8"))
        assert api_key_format.parse(data["api_key"]) is not None, host
        assert data["api_key"] not in result.stdout + result.stderr, host
        assert data["hub_cert"] == str(home / "tls" / "hub.pem"), host
        assert pem_fingerprint((home / "tls" / "hub.pem").read_text(encoding="utf-8")) == (
            fingerprint
        ), host
        minted.add(data["api_key_id"])
    assert _live_key_ids(postgres_db) == hub_keys | minted
    assert len(minted - hub_keys) == 2


def test_login_refuses_host_outside_sans(
    narrow_san_hub: DaemonInstance, postgres_db: HubDatabase, tmp_path: Path
) -> None:
    """4.5.7: a dialed host outside the SANs fails the handshake before the password."""
    assert narrow_san_hub.cert_path is not None
    fingerprint = pem_fingerprint(narrow_san_hub.cert_path.read_text(encoding="utf-8"))
    prior = write_self_signed_pair(tmp_path / "prior-tls", ["127.0.0.1"])
    home = _node_home(tmp_path, f"https://127.0.0.1:{narrow_san_hub.http_port}", prior=prior)
    before = _enrollment_files(home)
    hub_keys = _live_key_ids(postgres_db)

    result = _login(home, "--fingerprint", fingerprint)

    assert result.returncode != 0
    assert fingerprint in result.stdout
    assert "front_door.tls.sans" in result.stdout + result.stderr
    assert _enrollment_files(home) == before
    assert _live_key_ids(postgres_db) == hub_keys
