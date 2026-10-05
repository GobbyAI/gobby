"""Tests for `gobby auth login` and `gobby auth key` against an in-test hub."""

from __future__ import annotations

import json
import os
import platform
import socket
import ssl
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

import click
import pytest
import yaml
from click.testing import CliRunner, Result

from gobby.cli import auth_login
from gobby.cli.auth import auth
from gobby.utils import api_key_format
from gobby.utils.durable_file import exclusive_file_lock
from tests.fixtures.tls_certs import pem_fingerprint, write_self_signed_pair

pytestmark = pytest.mark.unit

EMAIL = "node-owner@example.com"
PASSWORD = "correct horse battery"
MACHINE_ID = "0b9c2f4e-6d1a-4e8b-9c3d-5f7a1b2c3d4e"
USER_ID = "4a6c8e0f-2b4d-4f6a-8c0e-1a3b5c7d9e1f"
PRIOR_KEY_ID = "9e8d7c6b-5a49-4382-a1b0-c9d8e7f6a5b4"
SESSION_TOKEN = "fake-hub-session"
LABEL = "node-label"


@dataclass(frozen=True)
class Recorded:
    method: str
    path: str
    body: dict[str, Any]
    cookie: str | None


@dataclass
class FakeHub:
    """A hub speaking the 4.2 routes: mint, password session, and owner revoke."""

    origin: str = ""
    pem: str = ""
    requests: list[Recorded] = field(default_factory=list)
    keys: dict[str, str] = field(default_factory=dict)
    live: set[str] = field(default_factory=lambda: {PRIOR_KEY_ID})
    revoked: list[str] = field(default_factory=list)
    reply: Callable[[dict[str, Any]], bytes] | None = None
    drop_bootstrap: bool = False
    delete_status: int = 200
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def paths(self, method: str | None = None) -> list[str]:
        return [r.path for r in self.requests if method is None or r.method == method]

    def handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        length = int(handler.headers.get("Content-Length", "0"))
        raw = handler.rfile.read(length) if length else b""
        body: dict[str, Any] = json.loads(raw) if raw else {}
        with self._lock:
            self.requests.append(Recorded(method, handler.path, body, handler.headers["Cookie"]))
        if method == "POST" and handler.path == "/api/auth/keys/bootstrap":
            self._bootstrap(handler, body)
        elif method == "POST" and handler.path == "/api/auth/login":
            if (body.get("email"), body.get("password")) != (EMAIL, PASSWORD):
                _respond(handler, 401, b'{"error": "invalid credentials"}')
                return
            cookie = f"gobby_session={SESSION_TOKEN}; HttpOnly; Path=/; SameSite=Lax"
            _respond(handler, 200, b"{}", {"Set-Cookie": cookie})
        elif method == "DELETE" and handler.path.startswith("/api/auth/keys/"):
            self._revoke(handler, handler.path.rsplit("/", 1)[1])
        else:
            _respond(handler, 404, b"{}")

    def _bootstrap(self, handler: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        if (body.get("email"), body.get("password")) != (EMAIL, PASSWORD):
            _respond(handler, 401, b'{"error": "invalid credentials"}')
            return
        if self.drop_bootstrap:
            handler.close_connection = True
            return
        key = api_key_format.generate()
        key_id = str(uuid.uuid4())
        with self._lock:
            self.keys[key_id] = key
            self.live.add(key_id)
        minted = {
            "key": key,
            "key_id": key_id,
            "hint": api_key_format.hint(key),
            "user_id": USER_ID,
            "machine_id": body["machine_id"],
        }
        payload = self.reply(minted) if self.reply else json.dumps(minted).encode()
        _respond(handler, 200, payload, {"Cache-Control": "no-store"})

    def _revoke(self, handler: BaseHTTPRequestHandler, key_id: str) -> None:
        if handler.headers["Cookie"] != f"gobby_session={SESSION_TOKEN}":
            _respond(handler, 401, b'{"error": "unauthenticated"}')
            return
        if self.delete_status != 200:
            _respond(handler, self.delete_status, b'{"error": "unavailable"}')
            return
        with self._lock:
            if key_id not in self.live:
                _respond(handler, 404, b"{}")
                return
            self.live.discard(key_id)
            self.revoked.append(key_id)
        _respond(handler, 200, b"{}")


def _respond(
    handler: BaseHTTPRequestHandler,
    status: int,
    payload: bytes,
    headers: dict[str, str] | None = None,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(payload)


class _HubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, hub: FakeHub) -> None:
        self.hub = hub
        super().__init__(("127.0.0.1", 0), _HubHandler)


class _HubHandler(BaseHTTPRequestHandler):
    server: _HubServer

    def do_POST(self) -> None:
        self.server.hub.handle(self, "POST")

    def do_DELETE(self) -> None:
        self.server.hub.handle(self, "DELETE")

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


@contextmanager
def _serve(
    hub: FakeHub, tmp_path: Path, *, tls: bool, host: str = "127.0.0.1"
) -> Iterator[FakeHub]:
    server = _HubServer(hub)
    if tls:
        cert = write_self_signed_pair(tmp_path / "hub-tls", ["127.0.0.1", "localhost", "::1"])
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, cert.with_suffix(".key"))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        hub.pem = cert.read_text(encoding="utf-8")
    port = server.server_address[1]
    hub.origin = f"{'https' if tls else 'http'}://{host}:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield hub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@dataclass(frozen=True)
class Node:
    home: Path
    bootstrap: Path
    pem: Path
    prior_key: str

    def read(self) -> dict[str, Any]:
        loaded: dict[str, Any] = yaml.safe_load(self.bootstrap.read_text(encoding="utf-8"))
        return loaded

    def snapshot(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.home)): path.read_bytes()
            for path in sorted(self.home.rglob("*"))
            if path.is_file() and not path.name.endswith(".lock")
        }


def _node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    *,
    pinned: bool = True,
    data: dict[str, Any] | None = None,
) -> Node:
    home = tmp_path / "node"
    home.mkdir(parents=True)
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setattr(auth_login, "require_machine_id", lambda: MACHINE_ID)
    pem = home / "tls" / "hub.pem"
    prior_key = api_key_format.generate()
    mapping: dict[str, Any] = data or {
        "datastore_mode": "remote",
        "hub_daemon_url": origin,
        "api_key": prior_key,
        "api_key_id": PRIOR_KEY_ID,
    }
    if pinned and data is None:
        prior = write_self_signed_pair(tmp_path / "prior-tls", ["127.0.0.1"])
        pem.parent.mkdir(mode=0o700)
        pem.write_text(prior.read_text(encoding="utf-8"), encoding="utf-8")
        pem.chmod(0o600)
        mapping["hub_cert"] = str(pem)
    bootstrap = home / "bootstrap.yaml"
    bootstrap.write_text(yaml.safe_dump(mapping, sort_keys=False), encoding="utf-8")
    bootstrap.chmod(0o600)
    return Node(home, bootstrap, pem, prior_key)


def _login(*args: str, prompts: str = "") -> Result:
    return CliRunner().invoke(auth, ["login", "--email", EMAIL, *args], input=prompts)


def _no_network(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    dialed: list[object] = []

    def _refuse(address: object, *args: object, **kwargs: object) -> socket.socket:
        dialed.append(address)
        raise AssertionError(f"unexpected network call to {address}")

    monkeypatch.setattr(socket, "create_connection", _refuse)
    return dialed


@pytest.fixture
def tls_hub(tmp_path: Path) -> Iterator[FakeHub]:
    with _serve(FakeHub(), tmp_path, tls=True) as hub:
        yield hub


def _with(**changes: object) -> Callable[[dict[str, Any]], bytes]:
    return lambda minted: json.dumps({**minted, **changes}).encode()


def _without(name: str) -> Callable[[dict[str, Any]], bytes]:
    return lambda minted: json.dumps({k: v for k, v in minted.items() if k != name}).encode()


# Each malformed reply, and the key id its error may print, labelled unverified.
MALFORMED_REPLIES: dict[str, tuple[Callable[[dict[str, Any]], bytes], str | None]] = {
    "malformed-json": (lambda _minted: b"{not json", None),
    "missing-field": (_without("user_id"), "minted"),
    "unparseable-key": (_with(key="gobby_not-a-key"), "minted"),
    "hint-mismatch": (_with(hint="zzzz"), "minted"),
    "foreign-machine": (_with(machine_id=str(uuid.uuid4())), "minted"),
    "prior-key-id": (_with(key_id=PRIOR_KEY_ID), PRIOR_KEY_ID),
}


@pytest.mark.parametrize("case", sorted(MALFORMED_REPLIES))
def test_login_validates_enrollment_response(
    tls_hub: FakeHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """4.5.8: an unverifiable 2xx publishes nothing and revokes nothing; a valid one enrolls."""
    node = _node(tmp_path, monkeypatch, tls_hub.origin)
    before = node.snapshot()
    tls_hub.reply, labelled = MALFORMED_REPLIES[case]

    rejected = _login("--fingerprint", pem_fingerprint(tls_hub.pem), prompts=f"{PASSWORD}\n")

    (unverified_id,) = tls_hub.keys
    reported = unverified_id if labelled == "minted" else labelled
    assert rejected.exit_code != 0
    assert "unverified key may have been minted" in rejected.output
    assert "gobby auth key list" in rejected.output
    assert (f"{reported} (unverified)" in rejected.output) is (reported is not None)
    assert (unverified_id in rejected.output) is (labelled == "minted")
    for secret in (tls_hub.keys[unverified_id], node.prior_key, "gobby_not-a-key", "{not json"):
        assert secret not in rejected.output
    assert node.snapshot() == before
    assert tls_hub.paths() == ["/api/auth/keys/bootstrap"]
    assert PRIOR_KEY_ID in tls_hub.live

    tls_hub.reply = None
    result = _login(
        "--fingerprint", pem_fingerprint(tls_hub.pem), "--label", LABEL, prompts=f"{PASSWORD}\n"
    )

    assert result.exit_code == 0, result.output
    (key_id,) = set(tls_hub.keys) - {unverified_id}
    key = tls_hub.keys[key_id]
    assert tls_hub.requests[1].body == {
        "email": EMAIL,
        "password": PASSWORD,
        "machine_id": MACHINE_ID,
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "label": LABEL,
    }
    data = node.read()
    assert (data["api_key"], data["api_key_id"], data["hub_cert"]) == (key, key_id, str(node.pem))
    assert node.pem.read_text(encoding="utf-8") == tls_hub.pem
    assert oct(node.pem.stat().st_mode & 0o777) == oct(0o600)
    assert sorted(path.name for path in node.pem.parent.iterdir()) == ["hub.pem"]
    assert api_key_format.hint(key) in result.output
    assert key_id in result.output
    assert key not in result.output
    assert tls_hub.live == {PRIOR_KEY_ID, unverified_id, key_id}


def _fail_replace_for(target: Path, *, thread: str | None = None) -> AbstractContextManager[Any]:
    real_replace = os.replace

    def _replace(src: Any, dst: Any) -> None:
        if Path(dst) == target and thread in (None, threading.current_thread().name):
            raise OSError("bootstrap publication failed")
        real_replace(src, dst)

    return patch("gobby.utils.durable_file.os.replace", side_effect=_replace)


@pytest.mark.parametrize("first", ["A", "B"])
def test_concurrent_enrollments_keep_winner(
    tls_hub: FakeHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    """4.5.9: a failed login never restores over the concurrent winner's files."""
    node = _node(tmp_path, monkeypatch, tls_hub.origin)
    minted = threading.Barrier(2)
    first_holds = threading.Event()

    @contextmanager
    def _ordered_lock(path: Path) -> Iterator[None]:
        minted.wait(timeout=10)
        if threading.current_thread().name != first:
            assert first_holds.wait(timeout=10)
        with exclusive_file_lock(path):
            first_holds.set()
            yield

    monkeypatch.setattr(auth_login, "exclusive_file_lock", _ordered_lock)
    request = auth_login.LoginRequest(
        hub=None,
        email=EMAIL,
        fingerprint=pem_fingerprint(tls_hub.pem),
        label=LABEL,
        insecure=False,
    )
    prompts = auth_login.LoginPrompts(
        email=lambda: EMAIL, confirm=lambda _message: False, password=lambda: PASSWORD
    )
    outcomes: dict[str, object] = {}

    def _run() -> None:
        try:
            outcomes[threading.current_thread().name] = auth_login.enroll(request, prompts)
        except click.ClickException as exc:
            outcomes[threading.current_thread().name] = exc

    with _fail_replace_for(node.bootstrap, thread="A"):
        threads = [threading.Thread(target=_run, name=name) for name in ("A", "B")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    winner = outcomes["B"]
    assert isinstance(winner, auth_login.Enrollment)
    assert isinstance(outcomes["A"], click.ClickException)
    (loser_id,) = set(tls_hub.keys) - {winner.key_id}
    data = node.read()
    assert (data["api_key"], data["api_key_id"]) == (tls_hub.keys[winner.key_id], winner.key_id)
    assert data["hub_cert"] == str(node.pem)
    assert node.pem.read_text(encoding="utf-8") == tls_hub.pem
    assert tls_hub.revoked == [loser_id]
    assert tls_hub.live == {PRIOR_KEY_ID, winner.key_id}


def test_login_refuses_local_bootstrap_and_hub_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4.5.2: a local bootstrap or a re-homing --hub refuses before any network call."""
    files_home = tmp_path / "files"
    files_home.mkdir()
    local = _node(
        tmp_path,
        monkeypatch,
        "",
        data={"datastore_mode": "local", "files_home": str(files_home)},
    )
    before = local.snapshot()
    dialed = _no_network(monkeypatch)

    refused = _login("--hub", "https://127.0.0.1:60999", prompts=f"y\n{PASSWORD}\n")

    assert refused.exit_code != 0
    assert "datastore_mode: local" in refused.output
    assert "docs/guides/shared-stack.md" in refused.output
    assert local.snapshot() == before

    local.bootstrap.write_text(
        yaml.safe_dump({"datastore_mode": "remote", "hub_daemon_url": "https://hub.example:8443"}),
        encoding="utf-8",
    )
    remote_before = local.snapshot()

    mismatch = _login("--hub", "https://other.example:8443", prompts=f"y\n{PASSWORD}\n")

    assert mismatch.exit_code != 0
    assert "https://hub.example:8443" in mismatch.output
    assert "docs/guides/shared-stack.md" in mismatch.output
    assert local.snapshot() == remote_before
    assert dialed == []


def test_login_http_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """4.5.3: plaintext needs loopback or --insecure, never a fingerprint.

    An environment proxy never sees the enrollment: the hub client dials the origin directly.
    """
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    probes: list[object] = []

    def _probe(address: object, *args: object, **kwargs: object) -> str:
        probes.append(address)
        raise AssertionError("plain HTTP fetched a certificate")

    monkeypatch.setattr(ssl, "get_server_certificate", _probe)
    with _serve(FakeHub(), tmp_path, tls=False) as loopback:
        node = _node(tmp_path, monkeypatch, loopback.origin, pinned=False)
        before = node.snapshot()

        pinned = _login("--fingerprint", f"sha256:{'0' * 64}", prompts=f"{PASSWORD}\n")

        assert pinned.exit_code != 0
        assert "--fingerprint" in pinned.output
        assert node.snapshot() == before
        assert loopback.requests == []

        enrolled = _login(prompts=f"{PASSWORD}\n")

        assert enrolled.exit_code == 0, enrolled.output
        (key_id,) = loopback.keys
        data = node.read()
        assert (data["api_key"], data["api_key_id"]) == (loopback.keys[key_id], key_id)
        assert "hub_cert" not in data

    real_getaddrinfo = socket.getaddrinfo

    def _resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
        return real_getaddrinfo("127.0.0.1" if host == "hub.invalid" else host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)
    with _serve(FakeHub(), tmp_path / "remote", tls=False, host="hub.invalid") as remote:
        other = tmp_path / "remote"
        node = _node(other, monkeypatch, remote.origin, pinned=False)
        before = node.snapshot()

        refused = _login(prompts=f"{PASSWORD}\n")

        assert refused.exit_code != 0
        assert "--insecure" in refused.output
        assert node.snapshot() == before
        assert remote.requests == []

        insecure = _login("--insecure", prompts=f"{PASSWORD}\n")

        assert insecure.exit_code == 0, insecure.output
        (key_id,) = remote.keys
        data = node.read()
        assert (data["api_key"], data["api_key_id"]) == (remote.keys[key_id], key_id)
        assert "hub_cert" not in data
    assert probes == []


@contextmanager
def _stalled_listener() -> Iterator[tuple[int, list[bytes]]]:
    listener = socket.create_server(("127.0.0.1", 0))
    received: list[bytes] = []
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def _accept() -> None:
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            accepted.append(connection)
            connection.settimeout(0.1)
            try:
                received.append(connection.recv(65536))
            except TimeoutError:
                received.append(b"")

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], received
    finally:
        stop.set()
        thread.join(timeout=5)
        for connection in accepted:
            connection.close()
        listener.close()


def test_login_failures_preserve_prior_enrollment(
    tls_hub: FakeHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4.5.5: every pre-publication failure leaves the prior enrollment untouched."""
    node = _node(tmp_path, monkeypatch, tls_hub.origin)
    before = node.snapshot()
    fingerprint = pem_fingerprint(tls_hub.pem)

    declined = _login(prompts=f"n\n{PASSWORD}\n")
    assert declined.exit_code != 0
    assert fingerprint in declined.output
    assert tls_hub.requests == []

    mismatch = _login("--fingerprint", f"sha256:{'0' * 64}", prompts=f"{PASSWORD}\n")
    assert mismatch.exit_code != 0
    assert fingerprint in mismatch.output
    assert tls_hub.requests == []
    assert node.snapshot() == before

    rejected = _login("--fingerprint", fingerprint, prompts="wrong password\n")
    assert rejected.exit_code != 0
    assert "rejected" in rejected.output
    assert tls_hub.paths() == ["/api/auth/keys/bootstrap"]
    assert node.snapshot() == before

    tls_hub.drop_bootstrap = True
    dropped = _login("--fingerprint", fingerprint, prompts=f"{PASSWORD}\n")
    assert dropped.exit_code != 0
    assert "could not reach the hub" in dropped.output
    assert tls_hub.paths() == ["/api/auth/keys/bootstrap"] * 2
    assert node.snapshot() == before

    monkeypatch.setattr(auth_login, "NETWORK_TIMEOUT_SECONDS", 0.5)
    with _stalled_listener() as (port, received):
        stalled_origin = f"https://127.0.0.1:{port}"
        data = node.read()
        data["hub_daemon_url"] = stalled_origin
        node.bootstrap.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        stalled_before = node.snapshot()

        stalled = _login(prompts=f"y\n{PASSWORD}\n")

        assert stalled.exit_code != 0
        assert "could not reach the hub" in stalled.output
        assert len(received) == 1
        assert PASSWORD.encode() not in received[0]
        assert node.snapshot() == stalled_before
    assert tls_hub.paths() == ["/api/auth/keys/bootstrap"] * 2
    assert tls_hub.live == {PRIOR_KEY_ID, *tls_hub.keys}


class _FailAfterBootstrapReplace:
    """Fail the directory fsync or the readback that follows the bootstrap rename."""

    def __init__(self, bootstrap: Path, stage: str) -> None:
        self.bootstrap = bootstrap
        self.stage = stage
        self.replaced = False
        self.failed = False
        self._real_replace = os.replace
        self._real_fsync = os.fsync
        self._real_read_bytes = Path.read_bytes

    def _replace(self, src: Any, dst: Any) -> None:
        self._real_replace(src, dst)
        if Path(dst) == self.bootstrap:
            self.replaced = True

    def _fsync(self, fd: int) -> None:
        if self.stage == "fsync" and self.replaced and not self.failed:
            self.failed = True
            raise OSError("directory fsync failed")
        self._real_fsync(fd)

    def _read_bytes(self, target: Path) -> bytes:
        if self.stage == "readback" and target == self.bootstrap and self.replaced:
            if not self.failed:
                self.failed = True
                raise OSError("readback failed")
        return self._real_read_bytes(target)

    def __enter__(self) -> None:
        self._patches = [
            patch("gobby.utils.durable_file.os.replace", side_effect=self._replace),
            patch("gobby.utils.durable_file.os.fsync", side_effect=self._fsync),
            patch.object(Path, "read_bytes", autospec=True, side_effect=self._read_bytes),
        ]
        for active in self._patches:
            active.start()

    def __exit__(self, *exc: object) -> None:
        for active in reversed(self._patches):
            active.stop()


def _fail_bootstrap_then_restore(node: Node) -> AbstractContextManager[Any]:
    real_replace = os.replace
    failed: list[Path] = []

    def _replace(src: Any, dst: Any) -> None:
        if Path(dst) == node.bootstrap or (Path(dst) == node.pem and failed):
            failed.append(Path(dst))
            raise OSError(f"{Path(dst).name} replace failed")
        real_replace(src, dst)

    return patch("gobby.utils.durable_file.os.replace", side_effect=_replace)


def test_login_compensation_follows_publication_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4.5.6: the bootstrap rename decides between revoke-and-restore and keep."""
    with _serve(FakeHub(), tmp_path / "before", tls=True) as hub:
        node = _node(tmp_path / "before", monkeypatch, hub.origin)
        before = node.snapshot()
        with _fail_replace_for(node.bootstrap):
            result = _login("--fingerprint", pem_fingerprint(hub.pem), prompts=f"{PASSWORD}\n")
        (new_id,) = hub.keys
        assert result.exit_code != 0
        assert "bootstrap publication failed" in result.output
        assert node.snapshot() == before
        assert hub.revoked == [new_id]
        assert hub.live == {PRIOR_KEY_ID}
        login, delete = hub.requests[1:]
        assert (login.path, login.body) == (
            "/api/auth/login",
            {"email": EMAIL, "password": PASSWORD},
        )
        assert (delete.method, delete.path) == ("DELETE", f"/api/auth/keys/{new_id}")

    @contextmanager
    def _unlockable(path: Path) -> Iterator[None]:
        raise TimeoutError("durable file lock deadline exceeded")
        yield

    with _serve(FakeHub(), tmp_path / "lock", tls=True) as hub:
        node = _node(tmp_path / "lock", monkeypatch, hub.origin)
        before = node.snapshot()
        with monkeypatch.context() as patched:
            patched.setattr(auth_login, "exclusive_file_lock", _unlockable)
            result = _login("--fingerprint", pem_fingerprint(hub.pem), prompts=f"{PASSWORD}\n")
        (new_id,) = hub.keys
        assert result.exit_code != 0
        assert "lock deadline exceeded" in result.output
        assert node.snapshot() == before
        assert hub.revoked == [new_id]
        assert hub.live == {PRIOR_KEY_ID}

    for stage in ("fsync", "readback"):
        root = tmp_path / stage
        with _serve(FakeHub(), root, tls=True) as hub:
            node = _node(root, monkeypatch, hub.origin)
            with _FailAfterBootstrapReplace(node.bootstrap, stage):
                result = _login("--fingerprint", pem_fingerprint(hub.pem), prompts=f"{PASSWORD}\n")
            (new_id,) = hub.keys
            assert result.exit_code != 0, stage
            assert new_id in result.output, stage
            data = node.read()
            assert (data["api_key"], data["api_key_id"]) == (hub.keys[new_id], new_id), stage
            assert node.pem.read_text(encoding="utf-8") == hub.pem, stage
            assert hub.paths() == ["/api/auth/keys/bootstrap"], stage
            assert hub.live == {PRIOR_KEY_ID, new_id}, stage

    with _serve(FakeHub(), tmp_path / "restore", tls=True) as hub:
        node = _node(tmp_path / "restore", monkeypatch, hub.origin)
        prior_bootstrap = node.bootstrap.read_bytes()
        with _fail_bootstrap_then_restore(node):
            result = _login("--fingerprint", pem_fingerprint(hub.pem), prompts=f"{PASSWORD}\n")
        (new_id,) = hub.keys
        assert result.exit_code != 0
        assert new_id in result.output
        assert "hub.pem replace failed" in result.output
        assert str(node.pem) in result.output
        assert node.bootstrap.read_bytes() == prior_bootstrap
        assert hub.paths() == ["/api/auth/keys/bootstrap"]
        assert hub.live == {PRIOR_KEY_ID, new_id}

    with _serve(FakeHub(delete_status=500), tmp_path / "cleanup", tls=True) as hub:
        node = _node(tmp_path / "cleanup", monkeypatch, hub.origin)
        before = node.snapshot()
        with _fail_replace_for(node.bootstrap):
            result = _login("--fingerprint", pem_fingerprint(hub.pem), prompts=f"{PASSWORD}\n")
        (new_id,) = hub.keys
        assert result.exit_code != 0
        assert new_id in result.output
        assert "500" in result.output
        assert node.snapshot() == before
        assert hub.paths("DELETE") == [f"/api/auth/keys/{new_id}"]
        assert hub.live == {PRIOR_KEY_ID, new_id}


def test_key_show_prints_hint_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """4.5.4: `gobby auth key --show` prints the hint and id, never the key."""
    node = _node(tmp_path, monkeypatch, "https://hub.example:8443", pinned=False)

    shown = CliRunner().invoke(auth, ["key", "--show"])

    assert shown.exit_code == 0, shown.output
    assert api_key_format.hint(node.prior_key) in shown.output
    assert PRIOR_KEY_ID in shown.output
    assert node.prior_key not in shown.output

    node.bootstrap.write_text(
        yaml.safe_dump({"datastore_mode": "remote", "hub_daemon_url": "https://hub.example:8443"}),
        encoding="utf-8",
    )
    missing = CliRunner().invoke(auth, ["key", "--show"])

    assert missing.exit_code != 0
    assert "gobby auth login" in missing.output
