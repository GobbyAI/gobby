"""Contracts for the loopback provider edge that the communications and voice recordings reach."""

from __future__ import annotations

import hashlib
import imaplib
import json
import smtplib
import socket
import ssl
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from tests.contracts.comms.fake_edge import FakeEdge

REPO_ROOT = Path(__file__).resolve().parents[3]
LOOPBACK_PROBE = REPO_ROOT / "tests" / "fixtures" / "loopback_probe.py"
LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_NO_PROXY = "127.0.0.1,localhost,::1"
PROVIDER_HOSTS = (
    "slack.com",
    "api.telegram.org",
    "login.microsoftonline.com",
    "api.botframework.com",
    "login.botframework.com",
    "api.twilio.com",
    "discord.com",
)
UNLISTED_HOST = "upload.corpus.test"
TEAMS_JWKS_HOST = "login.botframework.com"
TEAMS_JWKS_URL = f"https://{TEAMS_JWKS_HOST}/v1/.well-known/keys"
MAIL_USER = "corpus-mailbox"
MAIL_PASSWORD = "corpus-mail-password"
EDGE_BEARER = "corpus-edge-bearer"
ATTACHMENT_BYTES = b"corpus attachment line one\n"


def _probe(target: str, out_file: Path) -> str:
    """Classify one TCP connect to ``target`` with the repository's loopback probe."""
    subprocess.run(
        [sys.executable, "-I", str(LOOPBACK_PROBE), target, str(out_file)],
        check=True,
        timeout=60,
    )
    result: dict[str, object] = json.loads(out_file.read_text(encoding="utf-8"))
    return str(result["class"])


def _primary_address() -> str:
    """The address this machine uses for its default route, found without sending a packet."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))
        return str(probe.getsockname()[0])


@contextmanager
def _listening_on(address: str) -> Iterator[int]:
    """Listen on one address at an ephemeral port for the life of the block, and yield the port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((address, 0))
        listener.listen()
        yield int(listener.getsockname()[1])


def _route_environment_through(monkeypatch: pytest.MonkeyPatch, edge: FakeEdge) -> None:
    """Point this process at the edge: its proxy, the loopback bypass and the test authority."""
    host, port = edge.address
    for name in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, f"http://{host}:{port}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, LOOPBACK_NO_PROXY)
    monkeypatch.setenv("SSL_CERT_FILE", str(edge.ca_path()))


@pytest.mark.integration
def test_fake_edge_listens_on_loopback_only(tmp_path: Path) -> None:
    assert LOOPBACK_PROBE.is_file(), f"the loopback probe does not exist at {LOOPBACK_PROBE}"
    edge = FakeEdge(tmp_path)
    edge.start()
    try:
        host, port = edge.address
        assert port != 0, "the fake edge bound no port"
        assert host == LOOPBACK_HOST, f"the fake edge bound {host!r}, not the loopback address"
        assert _probe(f"{host}:{port}", tmp_path / "loopback.json") == "connected", (
            "the loopback probe found no listener on the fake edge"
        )
        primary = _primary_address()
        assert primary != LOOPBACK_HOST, "this machine has no non-loopback address to probe"
        with _listening_on(primary) as control_port:
            control = _probe(f"{primary}:{control_port}", tmp_path / "control.json")
            assert control == "connected", (
                f"a control listener on the primary address answered {control!r}, "
                "so a refused probe of the edge would prove nothing"
            )
        primary_class = _probe(f"{primary}:{port}", tmp_path / "primary.json")
        assert primary_class == "refused", (
            f"a non-loopback probe of the fake edge answered {primary_class!r}, not refused"
        )
    finally:
        edge.stop()


@pytest.mark.integration
def test_provider_hosts_route_to_the_edge_and_are_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edge = FakeEdge(tmp_path)
    edge.start()
    try:
        port = edge.address[1]
        assert port != 0, "the fake edge bound no port"
        _route_environment_through(monkeypatch, edge)
        with httpx.Client(timeout=10.0) as client:
            for provider in PROVIDER_HOSTS:
                edge.clear()
                response = client.get(f"https://{provider}/")
                assert response.status_code == 200, (
                    f"{provider} answered {response.status_code} through the edge"
                )
                arrivals = [hit["host"] for hit in edge.hits()]
                assert arrivals == [provider], f"{provider} logged arrivals {arrivals}"
            edge.clear()
            response = client.get(f"https://{UNLISTED_HOST}/upload")
            assert response.status_code == 200, (
                f"{UNLISTED_HOST} answered {response.status_code} through the edge"
            )
            arrivals = [hit["host"] for hit in edge.hits()]
            assert arrivals == [UNLISTED_HOST], f"{UNLISTED_HOST} logged arrivals {arrivals}"
    finally:
        edge.stop()


@pytest.mark.integration
def test_provider_requests_are_logged_with_what_they_send_and_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edge = FakeEdge(tmp_path)
    edge.start()
    try:
        assert edge.address[1] != 0, "the fake edge bound no port"
        _route_environment_through(monkeypatch, edge)
        with httpx.Client(timeout=10.0) as client:
            sent = client.post(
                "https://slack.com/api/chat.postMessage",
                json={"channel": "C0CORPUS0001", "text": "corpus message one"},
                headers={"Authorization": f"Bearer {EDGE_BEARER}"},
            )
            uploaded = client.put(
                f"https://{UNLISTED_HOST}/upload/corpus-1",
                content=ATTACHMENT_BYTES,
                headers={"Content-Type": "text/plain"},
            )
        assert (sent.status_code, uploaded.status_code) == (200, 200), (
            f"the edge answered {sent.status_code} and {uploaded.status_code}"
        )
        hits = edge.hits()
    finally:
        edge.stop()
    assert hits == [
        {
            "host": "slack.com",
            "method": "POST",
            "path": "/api/chat.postMessage",
            "semantic": {
                "destination": "C0CORPUS0001",
                "text": "corpus message one",
                "filename": "",
                "payload_sha256": "",
            },
        },
        {
            "host": UNLISTED_HOST,
            "method": "PUT",
            "path": "/upload/corpus-1",
            "semantic": {
                "destination": "",
                "text": "",
                "filename": "",
                "payload_sha256": hashlib.sha256(ATTACHMENT_BYTES).hexdigest(),
            },
        },
    ], f"the edge logged {hits}"
    assert EDGE_BEARER not in json.dumps(hits), "the edge log keeps a request credential"


@pytest.mark.integration
def test_urllib_jwks_fetch_goes_through_the_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edge = FakeEdge(tmp_path)
    edge.start()
    try:
        assert edge.address[1] != 0, "the fake edge bound no port"
        _route_environment_through(monkeypatch, edge)
        opener = urllib.request.build_opener()
        with opener.open(TEAMS_JWKS_URL, timeout=10) as reply:
            document = json.loads(reply.read().decode("utf-8"))
        assert isinstance(document.get("keys"), list), "the JWKS reply carries no key list"
        arrivals = [hit["host"] for hit in edge.hits()]
        assert arrivals == [TEAMS_JWKS_HOST], f"the JWKS fetch logged arrivals {arrivals}"
    finally:
        edge.stop()


@pytest.mark.integration
def test_email_tls_login_and_starttls_succeed_against_loopback(tmp_path: Path) -> None:
    edge = FakeEdge(tmp_path)
    edge.start()
    try:
        imap_host, imap_port = edge.listener("imap")
        smtp_host, smtp_port = edge.listener("smtp")
        assert imap_port != 0 and smtp_port != 0, "the fake edge serves no email listener"
        context = ssl.create_default_context(cafile=str(edge.ca_path()))
        with imaplib.IMAP4_SSL(imap_host, imap_port, ssl_context=context) as imap:
            status, _ = imap.login(MAIL_USER, MAIL_PASSWORD)
        assert status == "OK", f"the IMAP login over TLS answered {status}"
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            code, _ = smtp.login(MAIL_USER, MAIL_PASSWORD)
        assert code == 235, f"the SMTP login after STARTTLS answered {code}"
    finally:
        edge.stop()
