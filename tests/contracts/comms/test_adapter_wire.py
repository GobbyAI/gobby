"""Contracts for the adapter wire recordings of the five provider-API-contract adapters.

Each fixture in ``adapter_wire/`` is named ``<provider>_<operation>.json`` and records one
operation of one Python adapter, driven in-process through the loopback provider edge with
responses scripted from the provider's published API contract. No fixture was recorded against
a real account, so none is an oracle. A fixture holds:

- ``provider`` and ``operation``: the adapter and the operation, matching the file name.
- ``capture``: ``exchange`` when the adapter ran the operation, ``refusal`` when the adapter
  refused before any provider request. An inbound webhook that the adapter verifies and parses
  in-process is an ``exchange`` with no provider request.
- ``verified``: always ``false``; ``oracle``: always ``false``.
- ``source_file``, ``source_test``: the adapter module and the existing adapter test whose shapes
  the recording follows (``source_test`` is a pytest node id, ``file::test`` or
  ``file::Class::test``); ``contract_ref``: the published provider contract the scripted
  responses are taken from.
- ``drive``: a non-empty object, the adapter call (channel config and the message, attachment or
  webhook payload).
- ``provider_contract``: an object whose ``responses`` list holds the scripted provider
  responses in the order the edge serves them, each with the ``host``, ``method`` and ``path``
  of the request it answers and its ``body``. A refusal scripts no response.
- ``exchanges``: the ordered provider requests, each with ``host``, ``method``, ``path`` and the
  sanitized ``semantic`` fields ``destination``, ``text``, ``filename`` and ``payload_sha256``.
- ``outcome``: the semantic result the adapter returned.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import copy
import datetime as dt
import email
import email.policy
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO
from unittest import mock
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import aioimaplib
import aiosmtplib
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from gobby.communications.adapters import email as email_adapter
from gobby.communications.adapters import slack as slack_adapter
from gobby.communications.adapters.base import BaseChannelAdapter
from gobby.communications.adapters.email import EmailAdapter
from gobby.communications.adapters.slack import SlackAdapter
from gobby.communications.adapters.sms import SMSAdapter
from gobby.communications.adapters.teams import TeamsAdapter
from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.models import ChannelConfig, CommsAttachment, CommsMessage
from tests.contracts.comms.fake_edge import (
    LOOPBACK,
    MAX_LINE_BYTES,
    TELEGRAM_BOT_MARKER,
    TELEGRAM_BOT_PATH,
    FakeEdge,
    _imap_reply,
    _read_body,
    _read_head,
    _Reply,
    _reply_from,
    _smtp_greeting,
    _SmtpSession,
    _write_reply,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
WIRE_DIR = Path(__file__).resolve().parent / "adapter_wire"
ADAPTER_DIR = REPO_ROOT / "src" / "gobby" / "communications" / "adapters"
ADAPTER_TEST_DIR = REPO_ROOT / "tests" / "communications"
RECORD_ENV = "GOBBY_RECORD_ADAPTER_WIRE"
PROVIDERS = ("slack", "telegram", "teams", "email", "sms")
OPERATIONS = ("send", "inbound", "attachment")
EXPECTED_FIXTURES = frozenset(f"{p}_{o}" for p in PROVIDERS for o in OPERATIONS)
PROVENANCE_KEYS = ("source_file", "source_test", "contract_ref", "oracle")
CAPTURE_KINDS = ("exchange", "refusal")
REFUSAL_FIXTURES = frozenset({"sms_attachment"})
SEMANTIC_FIELDS = ("destination", "text", "filename", "payload_sha256")


@dataclass(frozen=True)
class WireReplay:
    """What one replay of a fixture observed.

    ``exchanges`` are the provider requests the edge served, in arrival order, in the fixture's
    exchange shape. ``upstream`` lists every connect the adapter attempted to a non-loopback
    address. ``outcome`` is the semantic result the adapter returned.
    """

    exchanges: list[dict[str, Any]] = field(default_factory=list)
    upstream: list[str] = field(default_factory=list)
    outcome: Any = None


def replay_wire_fixture(fixture: dict[str, Any], edge_root: Path) -> WireReplay:
    """Drive the fixture's adapter operation through a fresh edge under ``edge_root``.

    The edge serves only the fixture's own ``provider_contract.responses``, in order; a request
    that does not match the next scripted response answers HTTP 404 (or an SMTP or IMAP
    refusal). Every connect to a non-loopback address, and every lookup of a non-loopback name,
    is recorded in ``upstream`` and refused.
    """
    drive = copy.deepcopy(fixture["drive"])
    responses = copy.deepcopy(fixture["provider_contract"]["responses"])
    _mint_teams_token(drive, responses)
    edge_root.mkdir(parents=True, exist_ok=True)
    edge = WireEdge(edge_root, responses, _mailbox(drive))
    upstream: list[str] = []
    edge.start()
    try:
        with (
            _edge_environment(edge),
            _email_redirects(edge, drive),
            _guard_upstream(upstream),
        ):
            outcome = asyncio.run(_drive_adapter(str(fixture["provider"]), drive, edge_root))
    finally:
        edge.stop()
    return WireReplay(exchanges=edge.exchanges(), upstream=upstream, outcome=outcome)


def wire_differences(fixture: dict[str, Any], replay: WireReplay) -> list[str]:
    """Every difference between a fixture and a replay, one line per differing leaf.

    Each line starts with the JSON pointer of the differing leaf followed by ``": "``, for
    example ``/exchanges/2/semantic/text: ...``, ``/outcome/message_id: ...`` or
    ``/upstream: ...``. A leaf that does not differ gets no line.
    """
    differences: list[str] = []
    _leaf_differences("/exchanges", fixture.get("exchanges", []), replay.exchanges, differences)
    _leaf_differences("/outcome", fixture.get("outcome"), replay.outcome, differences)
    if replay.upstream:
        differences.append(f"/upstream: the replay reached {replay.upstream}")
    return differences


def record_wire_fixtures(out_dir: Path, edge_root: Path) -> list[Path]:
    """Record every expected fixture into ``out_dir`` and return the written paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for stem, spec in sorted(wire_specs().items()):
        replay = replay_wire_fixture(spec, edge_root / stem)
        assert replay.upstream == [], f"{stem} reached past the edge: {replay.upstream}"
        refused = stem in REFUSAL_FIXTURES
        errored = isinstance(replay.outcome, dict) and "error" in replay.outcome
        assert errored == refused, f"{stem} recorded the unexpected outcome {replay.outcome!r}"
        scripted = spec["provider_contract"]["responses"]
        assert _signatures(replay.exchanges) == _signatures(scripted), (
            f"{stem} sent {_signatures(replay.exchanges)}, not the scripted {_signatures(scripted)}"
        )
        fixture = {
            "provider": spec["provider"],
            "operation": spec["operation"],
            "capture": "refusal" if refused else "exchange",
            "verified": False,
            "oracle": False,
            "source_file": spec["source_file"],
            "source_test": spec["source_test"],
            "contract_ref": spec["contract_ref"],
            "drive": spec["drive"],
            "provider_contract": spec["provider_contract"],
            "exchanges": replay.exchanges,
            "outcome": replay.outcome,
        }
        path = out_dir / f"{stem}.json"
        path.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


# Replay machinery: the per-fixture edge, the semantic fields, the guards and the adapter drive.

UNSCRIPTED_HTTP_REPLY = _Reply(404, "application/json", b'{"ok": false, "error": "unscripted"}')
LOOPBACK_NO_PROXY = "127.0.0.1,localhost,::1"
PROXY_VARIABLES = ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
DESTINATION_KEYS = ("channel", "channel_id", "chat_id", "To")
TEXT_KEYS = ("text", "initial_comment", "caption", "Body")
TEAMS_ACTIVITY_PATH = re.compile(r".*/v3/conversations/([^/]+)/activities")
IMAP_MAILBOX_COMMANDS = frozenset({"SELECT", "SEARCH", "FETCH", "STORE"})
TEAMS_TOKEN_MARKER = "@REPLAY_TEAMS_TOKEN@"
TEAMS_KEY_MARKERS = {"n": "@REPLAY_TEAMS_KEY_N@", "e": "@REPLAY_TEAMS_KEY_E@"}
FIXED_TIME = dt.datetime(2023, 11, 14, 22, 13, 20, tzinfo=dt.UTC)
ADAPTERS: dict[str, type[BaseChannelAdapter]] = {
    "slack": SlackAdapter,
    "telegram": TelegramAdapter,
    "teams": TeamsAdapter,
    "email": EmailAdapter,
    "sms": SMSAdapter,
}


def _empty_semantic() -> dict[str, str]:
    return dict.fromkeys(SEMANTIC_FIELDS, "")


def _digest(payload: bytes | None) -> str:
    return hashlib.sha256(payload).hexdigest() if payload else ""


def _decode_http_body(
    content_type: str, body: bytes
) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    """The named fields and the uploaded files (filename, bytes) of one request body."""
    kind = content_type.split(";", 1)[0].strip().lower()
    if not body:
        return {}, []
    if kind == "application/json":
        document = json.loads(body)
        return (document if isinstance(document, dict) else {}), []
    if kind == "application/x-www-form-urlencoded":
        return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True)), []
    if kind == "multipart/form-data":
        head = f"Content-Type: {content_type}\r\n\r\n".encode("latin-1")
        parsed = email.message_from_bytes(head + body, policy=email.policy.default)
        fields: dict[str, Any] = {}
        files: list[tuple[str, bytes]] = []
        for part in parsed.iter_parts():
            data = part.get_payload(decode=True)
            payload = data if isinstance(data, bytes) else b""
            filename = part.get_filename()
            if filename:
                files.append((filename, payload))
            else:
                name = part.get_param("name", header="content-disposition")
                fields[str(name)] = payload.decode("utf-8")
        return fields, files
    return {}, []


def _first_text(fields: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = fields.get(key)
        if isinstance(value, str | int) and not isinstance(value, bool):
            return str(value)
    return ""


def _http_semantics(method: str, path: str, headers: dict[str, str], body: bytes) -> dict[str, str]:
    """The sanitized semantic fields of one HTTP provider request."""
    fields, files = _decode_http_body(headers.get("content-type", ""), body)
    semantic = _empty_semantic()
    semantic["destination"] = _first_text(fields, DESTINATION_KEYS)
    activity = TEAMS_ACTIVITY_PATH.fullmatch(path)
    if not semantic["destination"] and activity is not None:
        semantic["destination"] = unquote(activity.group(1))
    semantic["text"] = _first_text(fields, TEXT_KEYS)
    payload: bytes | None = None
    if files:
        semantic["filename"], payload = files[0]
    else:
        semantic["filename"] = _first_text(fields, ("filename",))
        completed = fields.get("files")
        if isinstance(completed, str):
            listed = json.loads(completed)
            if isinstance(listed, list) and listed and isinstance(listed[0], dict):
                semantic["filename"] = str(listed[0].get("title", ""))
        cards = fields.get("attachments")
        if isinstance(cards, list) and cards and isinstance(cards[0], dict):
            payload = json.dumps(cards[0].get("content"), sort_keys=True).encode("utf-8")
        elif method == "PUT":
            payload = body
    semantic["payload_sha256"] = _digest(payload)
    return semantic


def _mail_semantics(message_bytes: bytes) -> dict[str, str]:
    """The sanitized semantic fields of one SMTP submission."""
    message = email.message_from_bytes(message_bytes, policy=email.policy.default)
    semantic = _empty_semantic()
    semantic["destination"] = str(message.get("To", ""))
    if not isinstance(message, EmailMessage):
        return semantic
    body = message.get_body(preferencelist=("plain",))
    if isinstance(body, EmailMessage):
        semantic["text"] = str(body.get_content())
    for attachment in message.iter_attachments():
        data = attachment.get_payload(decode=True)
        semantic["filename"] = str(attachment.get_filename() or "")
        semantic["payload_sha256"] = _digest(data if isinstance(data, bytes) else None)
        break
    return semantic


class WireEdge(FakeEdge):
    """A provider edge that serves one fixture's scripted responses and keeps each request.

    It reuses the loopback listeners and the test authority of ``FakeEdge``, but answers from
    the fixture, in order, instead of the corpus edge scripts, and keeps each request's semantic
    fields. The IMAP listener also answers the mailbox commands and the SMTP listener also
    accepts a submission.
    """

    def __init__(
        self, root: Path, responses: list[dict[str, Any]], mail: tuple[str, str] | None
    ) -> None:
        super().__init__(root)
        self._queue = list(responses)
        self._mailbox_login = mail
        self._exchanges: list[dict[str, Any]] = []
        self._exchange_lock = threading.Lock()

    def exchanges(self) -> list[dict[str, Any]]:
        """Every provider request so far, in arrival order, in the fixture's exchange shape."""
        with self._exchange_lock:
            return copy.deepcopy(self._exchanges)

    def take(
        self, host: str, method: str, path: str, semantic: dict[str, str]
    ) -> dict[str, Any] | None:
        """Keep one request, and pop the next scripted response when it answers that request."""
        with self._exchange_lock:
            self._exchanges.append(
                {"host": host, "method": method, "path": path, "semantic": semantic}
            )
            head = self._queue[0] if self._queue else None
            if head is not None and (head.get("host"), head.get("method"), head.get("path")) == (
                host,
                method,
                path,
            ):
                return self._queue.pop(0)
            return None

    def _mail_credentials(self) -> tuple[str, str] | None:
        return self._mailbox_login

    def _serve_tunnel(self, tls: ssl.SSLSocket, host: str) -> None:
        reader = tls.makefile("rb")
        try:
            while True:
                head = _read_head(reader)
                if head is None:
                    return
                request_line, headers = head
                method, _, rest = request_line.partition(" ")
                target = rest.rpartition(" ")[0] or rest
                body = _read_body(reader, headers)
                path = TELEGRAM_BOT_PATH.sub(TELEGRAM_BOT_MARKER, urlsplit(target).path or "/")
                scripted = self.take(
                    host, method, path, _http_semantics(method, path, headers, body)
                )
                reply = UNSCRIPTED_HTTP_REPLY if scripted is None else _reply_from(scripted)
                _write_reply(tls, reply)
                if headers.get("connection", "").lower() == "close":
                    return
        except (OSError, ssl.SSLError, ValueError):
            return
        finally:
            reader.close()

    def _imap_session(self, tls: ssl.SSLSocket, reader: BinaryIO) -> None:
        while True:
            line = reader.readline(MAX_LINE_BYTES)
            if not line:
                return
            tag, _, rest = line.decode("utf-8", "replace").rstrip("\r\n").partition(" ")
            command, _, arguments = rest.partition(" ")
            verb = command.upper()
            if verb == "LOGOUT":
                tls.sendall(f"* BYE corpus IMAP closing\r\n{tag} OK LOGOUT completed\r\n".encode())
                return
            if verb == "CLOSE":
                tls.sendall(f"{tag} OK CLOSE completed\r\n".encode())
            elif verb in IMAP_MAILBOX_COMMANDS:
                tls.sendall(self._imap_mailbox_reply(tag, verb, arguments))
            else:
                tls.sendall(_imap_reply(tag, verb, arguments, self._mailbox_login))

    def _imap_mailbox_reply(self, tag: str, verb: str, arguments: str) -> bytes:
        semantic = _empty_semantic()
        if verb == "SELECT":
            semantic["destination"] = arguments.strip('"')
        scripted = self.take(LOOPBACK, "IMAP", verb, semantic)
        if scripted is None:
            return f"{tag} NO unscripted {verb}\r\n".encode()
        body = str(scripted.get("body", ""))
        if verb == "FETCH":
            literal = body.encode("utf-8")
            number = arguments.split(" ", 1)[0]
            fetched = f"* {number} FETCH (RFC822 {{{len(literal)}}}\r\n".encode() + literal
            return fetched + f")\r\n{tag} OK FETCH completed\r\n".encode()
        untagged = "".join(f"{line}\r\n" for line in body.splitlines())
        return f"{untagged}{tag} OK {verb} completed\r\n".encode()

    def _serve_smtp_client(self, conn: socket.socket) -> None:
        assert self._authority is not None
        with conn:
            conn.sendall(b"220 corpus SMTP ready\r\n")
            session = _WireSmtpSession(
                conn, self._authority.context(LOOPBACK), self._mailbox_login, self
            )
            session.run()


class _WireSmtpSession(_SmtpSession):
    """The fake edge's SMTP session, extended with MAIL, RCPT and DATA for one submission."""

    def __init__(
        self,
        conn: socket.socket,
        context: ssl.SSLContext,
        mail: tuple[str, str] | None,
        edge: WireEdge,
    ) -> None:
        super().__init__(conn, context, mail)
        self._edge = edge

    def run(self) -> None:
        reader = self._conn.makefile("rb")
        try:
            while True:
                line = reader.readline(MAX_LINE_BYTES)
                if not line:
                    return
                verb, _, arguments = line.decode("utf-8", "replace").rstrip("\r\n").partition(" ")
                verb = verb.upper()
                if verb in ("EHLO", "HELO"):
                    self._sock.sendall(_smtp_greeting(self._secure))
                elif verb == "STARTTLS" and not self._secure:
                    self._sock.sendall(b"220 Ready to start TLS\r\n")
                    reader.close()
                    self._sock = self._context.wrap_socket(self._conn, server_side=True)
                    self._secure = True
                    reader = self._sock.makefile("rb")
                elif verb == "AUTH":
                    self._auth(reader, arguments)
                elif verb in ("MAIL", "RCPT", "NOOP", "RSET"):
                    self._sock.sendall(b"250 OK\r\n")
                elif verb == "DATA":
                    self._sock.sendall(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                    self._sock.sendall(self._submission(reader))
                elif verb == "QUIT":
                    self._sock.sendall(b"221 Bye\r\n")
                    return
                else:
                    self._sock.sendall(b"502 Command not implemented\r\n")
        except (OSError, ssl.SSLError, ValueError):
            return
        finally:
            reader.close()
            if self._sock is not self._conn:
                self._sock.close()

    def _submission(self, reader: BinaryIO) -> bytes:
        lines: list[bytes] = []
        while True:
            line = reader.readline(MAX_LINE_BYTES)
            if line in (b"", b".\r\n", b".\n"):
                break
            lines.append(line[1:] if line.startswith(b"..") else line)
        scripted = self._edge.take(LOOPBACK, "SMTP", "DATA", _mail_semantics(b"".join(lines)))
        if scripted is None:
            return b"554 5.0.0 unscripted submission\r\n"
        return f"{scripted.get('body', '250 OK')}\r\n".encode()


def _mailbox(drive: dict[str, Any]) -> tuple[str, str] | None:
    mailbox = drive.get("mailbox")
    if not isinstance(mailbox, dict):
        return None
    return str(mailbox["user"]), str(mailbox["password"])


def _is_loopback(host: object) -> bool:
    if host is None or host == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


@contextlib.contextmanager
def _guard_upstream(upstream: list[str]) -> Iterator[None]:
    """Record and refuse every connect to, and every lookup of, a non-loopback address."""
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def connect(sock: socket.socket, address: Any) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address[0]):
            upstream.append(f"{address[0]}:{address[1]}")
            raise ConnectionRefusedError(f"the adapter wire replay refused {address[0]}")
        real_connect(sock, address)

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode("idna") if isinstance(host, bytes) else host
        if not _is_loopback(name):
            upstream.append(f"{name}:{port}")
            raise socket.gaierror(socket.EAI_NONAME, f"the adapter wire replay refused {name}")
        return real_getaddrinfo(host, port, *args, **kwargs)

    with (
        mock.patch.object(socket.socket, "connect", connect),
        mock.patch.object(socket, "getaddrinfo", getaddrinfo),
    ):
        yield


@contextlib.contextmanager
def _edge_environment(edge: FakeEdge) -> Iterator[None]:
    """Point HTTPS at the edge's proxy and trust its test authority, then restore."""
    host, port = edge.address
    with mock.patch.dict(os.environ):
        for name in PROXY_VARIABLES:
            os.environ.pop(name, None)
        for name in ("HTTPS_PROXY", "https_proxy"):
            os.environ[name] = f"http://{host}:{port}"
        for name in ("NO_PROXY", "no_proxy"):
            os.environ[name] = LOOPBACK_NO_PROXY
        os.environ["SSL_CERT_FILE"] = str(edge.ca_path())
        yield


@contextlib.contextmanager
def _email_redirects(edge: FakeEdge, drive: dict[str, Any]) -> Iterator[None]:
    """Send the email adapter's SMTP and IMAP clients to the edge listeners.

    The adapter keeps its configured ports (587 selects STARTTLS, 993 is implicit TLS), and
    only the connect port is moved to the edge. The generated Message-ID is pinned to the
    drive's value so that the outcome is the same on every replay.
    """
    smtp_port = edge.listener("smtp")[1]
    imap_port = edge.listener("imap")[1]
    real_smtp = aiosmtplib.SMTP
    real_imap = aioimaplib.IMAP4_SSL
    message_id = str(drive.get("message_id", ""))

    def smtp(**kwargs: Any) -> Any:
        return real_smtp(**{**kwargs, "port": smtp_port})

    def imap(**kwargs: Any) -> Any:
        return real_imap(**{**kwargs, "port": imap_port})

    def make_msgid(domain: str | None = None) -> str:
        return message_id

    with (
        mock.patch.object(aiosmtplib, "SMTP", smtp),
        mock.patch.object(aioimaplib, "IMAP4_SSL", imap),
        mock.patch.object(email_adapter, "make_msgid", make_msgid),
    ):
        yield


def _mint_teams_token(drive: dict[str, Any], responses: list[dict[str, Any]]) -> None:
    """Sign the drive's Teams claims with a key minted for this replay.

    The fixture stores the claims and a key-set body with marker values; the key pair exists
    only for one replay, so no private key is committed and the replay stays byte-stable.
    """
    token_spec = drive.get("teams_token")
    if not isinstance(token_spec, dict):
        return
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    for response in responses:
        body = response.get("body")
        for entry in body.get("keys", []) if isinstance(body, dict) else []:
            for field_name, marker in TEAMS_KEY_MARKERS.items():
                if entry.get(field_name) == marker:
                    entry[field_name] = public[field_name]
    token = jwt.encode(
        dict(token_spec["claims"]),
        key,
        algorithm="RS256",
        headers={"kid": str(token_spec["kid"])},
    )
    headers = drive["webhook"]["headers"]
    for name, value in headers.items():
        headers[name] = str(value).replace(TEAMS_TOKEN_MARKER, token)


def _resolver(secrets: dict[str, str]) -> Callable[[str], str | None]:
    def resolve(reference: str) -> str | None:
        return secrets.get(reference.removeprefix("$secret:"))

    return resolve


def _message(drive: dict[str, Any]) -> CommsMessage:
    spec = drive["message"]
    return CommsMessage(
        id="corpus-wire-message",
        channel_id="corpus-wire-channel",
        direction="outbound",
        content=str(spec["content"]),
        created_at=FIXED_TIME,
        content_type=str(spec.get("content_type", "text")),
        metadata_json=copy.deepcopy(spec.get("metadata", {})),
    )


def _inbound(message: CommsMessage) -> dict[str, Any]:
    """The semantic projection of one parsed inbound message: no clock or generated id."""
    return {
        "content": message.content,
        "content_type": message.content_type,
        "identity_id": message.identity_id,
        "platform_message_id": message.platform_message_id,
        "platform_thread_id": message.platform_thread_id or None,
        "platform_channel_id": message.metadata_json.get("platform_channel_id"),
        "external_username": message.metadata_json.get("external_username"),
    }


async def _drive_adapter(provider: str, drive: dict[str, Any], root: Path) -> dict[str, Any]:
    """Run the drive's adapter call and return its semantic outcome, or the refusal."""
    adapter = ADAPTERS[provider]()
    resolve = _resolver(dict(drive.get("secrets", {})))
    channel = drive["channel"]
    config = ChannelConfig(
        id="corpus-wire-channel",
        channel_type=provider,
        name=str(channel["name"]),
        enabled=True,
        config_json=copy.deepcopy(channel["config"]),
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
    )
    try:
        if drive.get("initialize", True):
            await adapter.initialize(config, resolve)
        call = drive["call"]
        if call == "send_message":
            return {"message_id": await adapter.send_message(_message(drive))}
        if call == "send_attachment":
            message = _message(drive)
            spec = drive["attachment"]
            data = base64.b64decode(str(spec["content_base64"]))
            path = root / "attachment" / str(spec["filename"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            attachment = CommsAttachment(
                id="corpus-wire-attachment",
                message_id=message.id,
                filename=str(spec["filename"]),
                content_type=str(spec["content_type"]),
                size_bytes=len(data),
                machine_id="corpus-wire-machine",
                platform_url=spec.get("platform_url"),
            )
            return {"message_id": await adapter.send_attachment(message, attachment, path)}
        if call == "poll":
            return {"messages": [_inbound(m) for m in await adapter.poll()]}
        if call == "webhook":
            return _deliver_webhook(provider, adapter, drive["webhook"], resolve)
        raise ValueError(f"the drive names the unknown call {call!r}")
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        await adapter.shutdown()


def _deliver_webhook(
    provider: str,
    adapter: BaseChannelAdapter,
    webhook: dict[str, Any],
    resolve: Callable[[str], str | None],
) -> dict[str, Any]:
    """Verify and parse one inbound webhook; Slack's clock is frozen at the drive's instant."""
    body = str(webhook["body"]).encode("utf-8")
    headers = {str(k): str(v) for k, v in webhook["headers"].items()}
    secret_ref = webhook.get("secret")
    secret = (resolve(str(secret_ref)) or "") if secret_ref else ""
    frozen = webhook.get("frozen_time")
    clock = (
        mock.patch.object(slack_adapter, "time", SimpleNamespace(time=lambda: float(frozen)))
        if provider == "slack" and frozen is not None
        else contextlib.nullcontext()
    )
    with clock:
        verified = adapter.verify_webhook(body, headers, secret)
        messages = adapter.parse_webhook(body, headers)
    return {"verified": verified, "messages": [_inbound(m) for m in messages]}


def _leaf_differences(pointer: str, expected: Any, actual: Any, out: list[str]) -> None:
    """Append one line per differing leaf under ``pointer``, index by index and key by key."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual), key=str):
            child = f"{pointer}/{str(key).replace('~', '~0').replace('/', '~1')}"
            if key not in actual:
                out.append(f"{child}: missing; the fixture holds {expected[key]!r}")
            elif key not in expected:
                out.append(f"{child}: unexpected {actual[key]!r}")
            else:
                _leaf_differences(child, expected[key], actual[key], out)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        for index in range(max(len(expected), len(actual))):
            child = f"{pointer}/{index}"
            if index >= len(actual):
                out.append(f"{child}: missing; the fixture holds {expected[index]!r}")
            elif index >= len(expected):
                out.append(f"{child}: unexpected {actual[index]!r}")
            else:
                _leaf_differences(child, expected[index], actual[index], out)
        return
    if type(expected) is not type(actual) or expected != actual:
        out.append(f"{pointer}: the fixture holds {expected!r}, the replay gave {actual!r}")


def _fixture_paths() -> dict[str, Path]:
    return {path.stem: path for path in sorted(WIRE_DIR.glob("*.json"))}


def _load_fixtures() -> dict[str, dict[str, Any]]:
    """Every fixture, keyed by file stem, after asserting the exact provider x operation set."""
    paths = _fixture_paths()
    missing = sorted(EXPECTED_FIXTURES - paths.keys())
    extra = sorted(paths.keys() - EXPECTED_FIXTURES)
    assert len(paths) == len(EXPECTED_FIXTURES), (
        f"{WIRE_DIR} holds {len(paths)} fixtures, not {len(EXPECTED_FIXTURES)}; "
        f"missing {missing}, unexpected {extra}"
    )
    assert not missing and not extra, f"missing fixtures {missing}, unexpected fixtures {extra}"
    fixtures: dict[str, dict[str, Any]] = {}
    for stem, path in paths.items():
        document = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict), f"{path} is not a JSON object"
        fixtures[stem] = document
    return fixtures


def _defines_test(source: str, node_path: list[str]) -> bool:
    """Whether ``source`` defines the test at ``node_path`` (classes, then the function)."""
    *classes, function = node_path
    body: list[ast.stmt] = ast.parse(source).body
    for class_name in classes:
        found = [n for n in body if isinstance(n, ast.ClassDef) and n.name == class_name]
        if not found:
            return False
        body = found[0].body
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function
        for node in body
    )


def _pointers(differences: list[str]) -> set[str]:
    return {line.partition(": ")[0] for line in differences}


def _signatures(exchanges: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [(str(e.get("host")), str(e.get("method")), str(e.get("path"))) for e in exchanges]


def _synthetic_fixture() -> dict[str, Any]:
    """A fixture shape built in the test, independent of any recorded file."""
    return {
        "provider": "slack",
        "operation": "attachment",
        "capture": "exchange",
        "exchanges": [
            {
                "host": "slack.com",
                "method": "POST",
                "path": "/api/files.getUploadURLExternal",
                "semantic": {
                    "destination": "C0CORPUS0001",
                    "text": "",
                    "filename": "corpus.txt",
                    "payload_sha256": "",
                },
            },
            {
                "host": "files.slack.com",
                "method": "PUT",
                "path": "/upload/corpus",
                "semantic": {
                    "destination": "",
                    "text": "",
                    "filename": "corpus.txt",
                    "payload_sha256": "0" * 64,
                },
            },
            {
                "host": "slack.com",
                "method": "POST",
                "path": "/api/files.completeUploadExternal",
                "semantic": {
                    "destination": "C0CORPUS0001",
                    "text": "corpus attachment",
                    "filename": "corpus.txt",
                    "payload_sha256": "",
                },
            },
        ],
        "outcome": {"message_id": "F0CORPUS0001"},
    }


def _faithful_replay(fixture: dict[str, Any]) -> WireReplay:
    return WireReplay(
        exchanges=copy.deepcopy(fixture["exchanges"]),
        upstream=[],
        outcome=copy.deepcopy(fixture["outcome"]),
    )


def test_adapter_wire_covers_every_provider_and_operation() -> None:
    fixtures = _load_fixtures()
    for stem, fixture in fixtures.items():
        named = f"{fixture.get('provider')}_{fixture.get('operation')}"
        assert named == stem, f"{stem}.json declares provider and operation {named!r}"


def test_adapter_wire_fixtures_carry_provenance() -> None:
    fixtures = _load_fixtures()
    for stem, fixture in fixtures.items():
        absent = [key for key in PROVENANCE_KEYS if key not in fixture]
        assert not absent, f"{stem}.json lacks the provenance keys {absent}"
        assert fixture["oracle"] is False, f"{stem}.json does not declare oracle false"
        source_file = REPO_ROOT / str(fixture["source_file"])
        assert source_file.is_file(), f"{stem}.json names a missing source file {source_file}"
        assert source_file.parent == ADAPTER_DIR, (
            f"{stem}.json names {source_file}, which is not an adapter module"
        )
        test_path, *node_path = str(fixture["source_test"]).split("::")
        source_test = REPO_ROOT / test_path
        assert source_test.is_file() and source_test.is_relative_to(ADAPTER_TEST_DIR), (
            f"{stem}.json names {source_test}, which is not a communications test file"
        )
        assert node_path and all(node_path), (
            f"{stem}.json names the node id {fixture['source_test']!r}, which has no test name"
        )
        assert _defines_test(source_test.read_text(encoding="utf-8"), node_path), (
            f"{stem}.json names the test {'::'.join(node_path)!r}, "
            f"which {source_test} does not define"
        )
        contract_ref = str(fixture["contract_ref"])
        assert contract_ref.startswith("https://"), (
            f"{stem}.json cites {contract_ref!r}, not a published provider contract"
        )


def _label_problems(stem: str, fixture: dict[str, Any]) -> list[str]:
    """Every way a fixture's verified flag, capture label, exchanges or outcome is mislabelled."""
    problems: list[str] = []
    if fixture.get("verified") is not False:
        problems.append(f"{stem}.json does not declare verified false")
    capture = fixture.get("capture")
    if capture not in CAPTURE_KINDS:
        problems.append(f"{stem}.json declares the capture kind {capture!r}")
    exchanges = fixture.get("exchanges")
    if not isinstance(exchanges, list):
        return [*problems, f"{stem}.json has no exchanges list"]
    if stem in REFUSAL_FIXTURES and capture != "refusal":
        problems.append(f"{stem}.json is not labelled a refusal")
    if capture == "refusal":
        if exchanges != []:
            problems.append(f"the refusal {stem}.json records provider requests")
        outcome = fixture.get("outcome")
        if not (isinstance(outcome, dict) and outcome.get("error")):
            problems.append(f"the refusal {stem}.json records no error outcome")
    elif fixture.get("operation") != "inbound" and not exchanges:
        problems.append(f"{stem}.json records a {fixture.get('operation')} with no request")
    for exchange in exchanges:
        semantic = exchange.get("semantic")
        if not isinstance(semantic, dict):
            problems.append(f"{stem}.json has an exchange with no semantics")
        elif set(semantic) != set(SEMANTIC_FIELDS):
            problems.append(f"{stem}.json carries the semantic fields {sorted(semantic)}")
    return problems


def test_adapter_wire_labels_refusals_and_unverified_captures() -> None:
    fixtures = _load_fixtures()
    for stem, fixture in fixtures.items():
        problems = _label_problems(stem, fixture)
        assert problems == [], f"{stem}.json is mislabelled: {problems}"


def test_a_refusal_label_on_a_fixture_that_does_not_refuse_is_mislabelled() -> None:
    fixture = copy.deepcopy(_load_fixtures()["slack_send"])
    assert _label_problems("slack_send", fixture) == [], "slack_send.json is mislabelled"
    fixture["capture"] = "refusal"
    fixture["exchanges"] = []
    fixture["outcome"] = {"error": "corpus relabelled refusal"}
    problems = _label_problems("slack_send", fixture)
    assert problems, "a send relabelled as a refusal with no requests is accepted"


def _contract_problems(stem: str, fixture: dict[str, Any]) -> list[str]:
    """Every way a fixture's drive or scripted provider responses fall short of replaying it."""
    problems: list[str] = []
    drive = fixture.get("drive")
    if not (isinstance(drive, dict) and drive):
        problems.append(f"{stem}.json has no drive object")
    contract = fixture.get("provider_contract")
    if not isinstance(contract, dict):
        return [*problems, f"{stem}.json has no provider_contract object"]
    responses = contract.get("responses")
    if not isinstance(responses, list):
        return [*problems, f"{stem}.json has no provider_contract.responses list"]
    if fixture.get("capture") == "refusal" and responses != []:
        problems.append(f"the refusal {stem}.json scripts provider responses")
    for index, response in enumerate(responses):
        if not isinstance(response, dict):
            problems.append(f"{stem}.json response {index} is not an object")
            continue
        absent = [key for key in ("host", "method", "path", "body") if key not in response]
        if absent:
            problems.append(f"{stem}.json response {index} lacks {absent}")
    return problems


def test_adapter_wire_fixtures_carry_drive_and_provider_contract() -> None:
    fixtures = _load_fixtures()
    for stem, fixture in fixtures.items():
        problems = _contract_problems(stem, fixture)
        assert problems == [], f"{stem}.json cannot be replayed: {problems}"


@pytest.mark.parametrize("stem", ["slack_send", "slack_attachment", "telegram_send"])
def test_an_exchange_with_no_scripted_provider_response_cannot_be_replayed(stem: str) -> None:
    fixture = copy.deepcopy(_load_fixtures()[stem])
    assert fixture.get("capture") == "exchange", f"{stem}.json is not an exchange"
    assert _contract_problems(stem, fixture) == [], f"{stem}.json cannot be replayed"
    fixture["provider_contract"]["responses"] = []
    problems = _contract_problems(stem, fixture)
    assert problems, f"{stem}.json with no scripted provider response is accepted"


@pytest.mark.integration
def test_adapter_wire_recordings_are_offline(tmp_path: Path) -> None:
    fixtures = _load_fixtures()
    for stem, fixture in fixtures.items():
        replay = replay_wire_fixture(fixture, tmp_path / stem)
        assert replay.upstream == [], f"{stem} reached past the edge: {replay.upstream}"
        assert _signatures(replay.exchanges) == _signatures(fixture["exchanges"]), (
            f"{stem} sent {_signatures(replay.exchanges)}, "
            f"not the recorded {_signatures(fixture['exchanges'])}"
        )


@pytest.mark.integration
def test_adapter_wire_replay_matches_the_provider_contract(tmp_path: Path) -> None:
    fixtures = _load_fixtures()
    for stem, fixture in fixtures.items():
        replay = replay_wire_fixture(fixture, tmp_path / stem)
        assert replay.exchanges == fixture["exchanges"], (
            f"{stem} sent {replay.exchanges}, not the recorded {fixture['exchanges']}"
        )
        assert replay.outcome == fixture["outcome"], (
            f"{stem} returned {replay.outcome!r}, not the recorded {fixture['outcome']!r}"
        )
        assert wire_differences(fixture, replay) == [], f"{stem} differs from its fixture"


@pytest.mark.integration
def test_adapter_wire_replay_detects_a_changed_provider_response(tmp_path: Path) -> None:
    fixture = copy.deepcopy(_load_fixtures()["slack_send"])
    responses = fixture.get("provider_contract", {}).get("responses")
    assert isinstance(responses, list) and responses, "slack_send.json scripts no responses"
    posts = [r for r in responses if r.get("path") == "/api/chat.postMessage"]
    assert len(posts) == 1, f"slack_send.json scripts {len(posts)} chat.postMessage responses"
    body = posts[0].get("body")
    assert isinstance(body, dict) and "ts" in body, "slack_send.json scripts no message ts"
    body["ts"] = "9999999999.999999"
    replay = replay_wire_fixture(fixture, tmp_path / "slack_send")
    differences = wire_differences(fixture, replay)
    assert _pointers(differences) == {"/outcome/message_id"}, (
        f"a changed provider message ts gave the differences {differences}"
    )


def _swap_first_two(replay: WireReplay) -> None:
    replay.exchanges[0], replay.exchanges[1] = replay.exchanges[1], replay.exchanges[0]


def _set_semantic(name: str, value: str) -> Callable[[WireReplay], None]:
    def mutate(replay: WireReplay) -> None:
        replay.exchanges[2]["semantic"][name] = value

    return mutate


def _change_outcome(replay: WireReplay) -> None:
    replay.outcome["message_id"] = "F0CORPUS0002"


def _reach_upstream(replay: WireReplay) -> None:
    replay.upstream.append("203.0.113.10:443")


_SWAPPED_LEAVES = frozenset(
    f"/exchanges/{index}/{leaf}"
    for index in (0, 1)
    for leaf in ("host", "method", "path", "semantic/destination", "semantic/payload_sha256")
)


@pytest.mark.parametrize(
    ("mutation", "pointers"),
    [
        pytest.param(_swap_first_two, _SWAPPED_LEAVES, id="request-order"),
        pytest.param(
            _set_semantic("destination", "C0OTHER0001"),
            {"/exchanges/2/semantic/destination"},
            id="destination",
        ),
        pytest.param(
            _set_semantic("text", "another text"), {"/exchanges/2/semantic/text"}, id="text"
        ),
        pytest.param(
            _set_semantic("filename", "other.txt"),
            {"/exchanges/2/semantic/filename"},
            id="filename",
        ),
        pytest.param(
            _set_semantic("payload_sha256", "f" * 64),
            {"/exchanges/2/semantic/payload_sha256"},
            id="digest",
        ),
        pytest.param(_change_outcome, {"/outcome/message_id"}, id="outcome"),
        pytest.param(_reach_upstream, {"/upstream"}, id="upstream"),
    ],
)
def test_wire_differences_names_a_mutated_field(
    mutation: Callable[[WireReplay], None], pointers: frozenset[str] | set[str]
) -> None:
    fixture = _synthetic_fixture()
    replay = _faithful_replay(fixture)
    assert wire_differences(fixture, replay) == [], "a faithful replay gave differences"
    mutation(replay)
    differences = wire_differences(fixture, replay)
    assert differences, f"a mutation of {sorted(pointers)} gave no difference"
    assert _pointers(differences) == set(pointers), (
        f"a mutation of {sorted(pointers)} gave the differences {differences}, "
        "which name other leaves or miss one"
    )


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get(RECORD_ENV) != "1", reason=f"set {RECORD_ENV}=1 to re-record")
def test_record_adapter_wire(tmp_path: Path) -> None:
    written = record_wire_fixtures(WIRE_DIR, tmp_path)
    assert sorted(path.stem for path in written) == sorted(EXPECTED_FIXTURES)


# Recording specs: the drive and the scripted provider responses of each fixture. Responses
# follow each provider's published API reference (``contract_ref``); every value is synthetic.

ADAPTER_MODULE = "src/gobby/communications/adapters"
ADAPTER_TESTS = "tests/communications/adapters"
TEXT_ATTACHMENT = base64.b64encode(b"corpus attachment bytes\n").decode("ascii")
PNG_ATTACHMENT = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII="
)
SLACK_CHANNEL = "C0CORPUS0001"
SLACK_SECRETS = {
    "SLACK_BOT_TOKEN": "corpus-wire-slack-bot-token",
    "SLACK_SIGNING_SECRET": "corpus-wire-slack-signing-secret",
}
SLACK_CONFIG = {
    "bot_token": "$secret:SLACK_BOT_TOKEN",
    "signing_secret": "$secret:SLACK_SIGNING_SECRET",
    "default_destination": SLACK_CHANNEL,
}
SLACK_AUTH_TEST = {
    "host": "slack.com",
    "method": "POST",
    "path": "/api/auth.test",
    "body": {
        "ok": True,
        "url": "https://corpus-workspace.slack.com/",
        "team": "corpus-team",
        "user": "corpus-bot",
        "team_id": "T0CORPUS0001",
        "user_id": "U0CORPUS0001",
        "bot_id": "B0CORPUS0001",
    },
}
TELEGRAM_SECRETS = {"TELEGRAM_BOT_TOKEN": "1000001:corpus-wire-telegram-token"}
TELEGRAM_CONFIG = {"bot_token": "$secret:TELEGRAM_BOT_TOKEN"}
TELEGRAM_PATH = "/bot@TELEGRAM_TOKEN@"
TELEGRAM_CHAT = "1000002"
TELEGRAM_INITIALIZE = [
    {
        "host": "api.telegram.org",
        "method": "POST",
        "path": f"{TELEGRAM_PATH}/getMe",
        "body": {
            "ok": True,
            "result": {
                "id": 1000001,
                "is_bot": True,
                "first_name": "corpus",
                "username": "corpus_bot",
            },
        },
    },
    {
        "host": "api.telegram.org",
        "method": "POST",
        "path": f"{TELEGRAM_PATH}/setMyCommands",
        "body": {"ok": True, "result": True},
    },
    {
        "host": "api.telegram.org",
        "method": "POST",
        "path": f"{TELEGRAM_PATH}/deleteWebhook",
        "body": {"ok": True, "result": True, "description": "Webhook is already deleted"},
    },
]
TELEGRAM_PRIVATE_CHAT = {"id": 1000002, "type": "private", "first_name": "Corpus"}
TEAMS_APP_ID = "corpus-wire-teams-app-id"
TEAMS_SECRETS = {"TEAMS_APP_ID": TEAMS_APP_ID, "TEAMS_APP_PASSWORD": "corpus-wire-teams-password"}
TEAMS_CONFIG = {"app_id": "$secret:TEAMS_APP_ID", "app_password": "$secret:TEAMS_APP_PASSWORD"}
TEAMS_SERVICE_URL = "https://smba.trafficmanager.net/amer/"
TEAMS_CONVERSATION = "a:1corpus-conversation"
TEAMS_TOKEN = {
    "host": "login.microsoftonline.com",
    "method": "POST",
    "path": "/botframework.com/oauth2/v2.0/token",
    "body": {
        "token_type": "Bearer",
        "expires_in": 3600,
        "ext_expires_in": 3600,
        "access_token": "corpus-wire-teams-access-token",
    },
}
TEAMS_ACTIVITY_REPLY_PATH = f"/amer/v3/conversations/{TEAMS_CONVERSATION}/activities"
EMAIL_ADDRESS = "gobby@corpus.test"
EMAIL_SECRETS = {"EMAIL_PASSWORD": "corpus-wire-mail-password"}
EMAIL_MAILBOX = {"user": EMAIL_ADDRESS, "password": EMAIL_SECRETS["EMAIL_PASSWORD"]}
EMAIL_SMTP_CONFIG = {
    "smtp_host": LOOPBACK,
    "smtp_port": 587,
    "from_address": EMAIL_ADDRESS,
    "default_recipient": "operator@corpus.test",
    "password": "$secret:EMAIL_PASSWORD",
}
EMAIL_SUBMISSION = {
    "host": LOOPBACK,
    "method": "SMTP",
    "path": "DATA",
    "body": "250 2.0.0 Ok: queued as CORPUS0001",
}
SMS_ACCOUNT = "AC00000000000000000000000000000001"
SMS_SECRETS = {"TWILIO_AUTH_TOKEN": "corpus-wire-twilio-auth-token"}
SMS_WEBHOOK_URL = "https://gobby.corpus.test/api/comms/webhooks/corpus-sms"
SMS_CONFIG = {
    "auth_token": "$secret:TWILIO_AUTH_TOKEN",
    "account_sid": SMS_ACCOUNT,
    "from_number": "+15550000001",
    "webhook_url": SMS_WEBHOOK_URL,
}
SMS_MESSAGES_PATH = f"/2010-04-01/Accounts/{SMS_ACCOUNT}/Messages.json"


def _spec(
    stem: str,
    source: str,
    contract_ref: str,
    drive: dict[str, Any],
    responses: list[dict[str, Any]],
) -> dict[str, Any]:
    provider, _, operation = stem.partition("_")
    return {
        "provider": provider,
        "operation": operation,
        "source_file": f"{ADAPTER_MODULE}/{provider}.py",
        "source_test": source,
        "contract_ref": contract_ref,
        "drive": drive,
        "provider_contract": {"responses": responses},
    }


def _slack_signature(body: str, timestamp: str) -> str:
    secret = SLACK_SECRETS["SLACK_SIGNING_SECRET"].encode("utf-8")
    base = f"v0:{timestamp}:{body}".encode()
    return "v0=" + hmac.new(secret, base, hashlib.sha256).hexdigest()


def _twilio_signature(body: str) -> str:
    data = SMS_WEBHOOK_URL + "".join(f"{k}{v}" for k, v in sorted(parse_qsl(body)))
    secret = SMS_SECRETS["TWILIO_AUTH_TOKEN"].encode("utf-8")
    return base64.b64encode(hmac.new(secret, data.encode(), hashlib.sha1).digest()).decode()


def _slack_specs() -> list[dict[str, Any]]:
    event = {
        "type": "event_callback",
        "team_id": "T0CORPUS0001",
        "api_app_id": "A0CORPUS0001",
        "event": {
            "type": "message",
            "channel": SLACK_CHANNEL,
            "user": "U0CORPUS0002",
            "text": "corpus slack inbound",
            "ts": "1700000000.000300",
            "channel_type": "channel",
        },
        "event_id": "Ev0CORPUS0001",
        "event_time": 1700000000,
    }
    event_body = json.dumps(event, sort_keys=True)
    timestamp = "1700000000"
    channel = {"name": "corpus-slack", "config": SLACK_CONFIG}
    return [
        _spec(
            "slack_send",
            f"{ADAPTER_TESTS}/test_slack.py::test_send_message_success",
            "https://api.slack.com/methods/chat.postMessage",
            {
                "channel": channel,
                "secrets": SLACK_SECRETS,
                "call": "send_message",
                "message": {
                    "content": "corpus slack send",
                    "metadata": {"platform_destination": SLACK_CHANNEL},
                },
            },
            [
                SLACK_AUTH_TEST,
                {
                    "host": "slack.com",
                    "method": "POST",
                    "path": "/api/chat.postMessage",
                    "body": {
                        "ok": True,
                        "channel": SLACK_CHANNEL,
                        "ts": "1700000000.000100",
                        "message": {"text": "corpus slack send", "ts": "1700000000.000100"},
                    },
                },
            ],
        ),
        _spec(
            "slack_inbound",
            f"{ADAPTER_TESTS}/test_slack.py::test_parse_webhook_event_callback",
            "https://api.slack.com/apis/events-api",
            {
                "channel": channel,
                "secrets": SLACK_SECRETS,
                "initialize": False,
                "call": "webhook",
                "webhook": {
                    "body": event_body,
                    "headers": {
                        "content-type": "application/json",
                        "x-slack-request-timestamp": timestamp,
                        "x-slack-signature": _slack_signature(event_body, timestamp),
                    },
                    "secret": "$secret:SLACK_SIGNING_SECRET",
                    "frozen_time": 1700000030,
                },
            },
            [],
        ),
        _spec(
            "slack_attachment",
            f"{ADAPTER_TESTS}/test_slack.py::test_send_attachment_three_step_upload",
            "https://api.slack.com/methods/files.getUploadURLExternal",
            {
                "channel": channel,
                "secrets": SLACK_SECRETS,
                "call": "send_attachment",
                "message": {
                    "content": "corpus slack attachment",
                    "metadata": {"platform_destination": SLACK_CHANNEL},
                },
                "attachment": {
                    "filename": "corpus-report.txt",
                    "content_type": "text/plain",
                    "content_base64": TEXT_ATTACHMENT,
                },
            },
            [
                SLACK_AUTH_TEST,
                {
                    "host": "slack.com",
                    "method": "POST",
                    "path": "/api/files.getUploadURLExternal",
                    "body": {
                        "ok": True,
                        "upload_url": "https://files.slack.com/upload/v1/corpus-wire-1",
                        "file_id": "F0CORPUS0001",
                    },
                },
                {
                    "host": "files.slack.com",
                    "method": "PUT",
                    "path": "/upload/v1/corpus-wire-1",
                    "content_type": "text/plain",
                    "body": "OK - 24",
                },
                {
                    "host": "slack.com",
                    "method": "POST",
                    "path": "/api/files.completeUploadExternal",
                    "body": {
                        "ok": True,
                        "files": [
                            {
                                "id": "F0CORPUS0001",
                                "title": "corpus-report.txt",
                                "shares": {
                                    "public": {SLACK_CHANNEL: [{"ts": "1700000000.000200"}]}
                                },
                            }
                        ],
                    },
                },
            ],
        ),
    ]


def _telegram_specs() -> list[dict[str, Any]]:
    channel = {"name": "corpus-telegram", "config": TELEGRAM_CONFIG}
    chat = {"id": -1001000000001, "type": "supergroup", "title": "corpus"}
    return [
        _spec(
            "telegram_send",
            f"{ADAPTER_TESTS}/test_telegram.py::test_send_message_basic",
            "https://core.telegram.org/bots/api#sendmessage",
            {
                "channel": channel,
                "secrets": TELEGRAM_SECRETS,
                "call": "send_message",
                "message": {
                    "content": "corpus telegram send",
                    "metadata": {"platform_destination": TELEGRAM_CHAT},
                },
            },
            [
                *TELEGRAM_INITIALIZE,
                {
                    "host": "api.telegram.org",
                    "method": "POST",
                    "path": f"{TELEGRAM_PATH}/sendMessage",
                    "body": {
                        "ok": True,
                        "result": {
                            "message_id": 101,
                            "date": 1700000000,
                            "chat": chat,
                            "text": "corpus telegram send",
                        },
                    },
                },
            ],
        ),
        _spec(
            "telegram_inbound",
            f"{ADAPTER_TESTS}/test_telegram.py::test_poll",
            "https://core.telegram.org/bots/api#getupdates",
            {"channel": channel, "secrets": TELEGRAM_SECRETS, "call": "poll"},
            [
                *TELEGRAM_INITIALIZE,
                {
                    "host": "api.telegram.org",
                    "method": "GET",
                    "path": f"{TELEGRAM_PATH}/getUpdates",
                    "body": {
                        "ok": True,
                        "result": [
                            {
                                "update_id": 5001,
                                "message": {
                                    "message_id": 201,
                                    "date": 1700000000,
                                    "chat": TELEGRAM_PRIVATE_CHAT,
                                    "from": {
                                        "id": 1000002,
                                        "is_bot": False,
                                        "first_name": "Corpus",
                                        "username": "corpus_user",
                                    },
                                    "text": "corpus telegram inbound",
                                },
                            }
                        ],
                    },
                },
            ],
        ),
        _spec(
            "telegram_attachment",
            f"{ADAPTER_TESTS}/test_telegram.py::test_send_attachment_uses_send_photo_for_images",
            "https://core.telegram.org/bots/api#sendphoto",
            {
                "channel": channel,
                "secrets": TELEGRAM_SECRETS,
                "call": "send_attachment",
                "message": {
                    "content": "corpus telegram attachment",
                    "metadata": {"platform_destination": TELEGRAM_CHAT},
                },
                "attachment": {
                    "filename": "corpus-pixel.png",
                    "content_type": "image/png",
                    "content_base64": PNG_ATTACHMENT,
                },
            },
            [
                *TELEGRAM_INITIALIZE,
                {
                    "host": "api.telegram.org",
                    "method": "POST",
                    "path": f"{TELEGRAM_PATH}/sendPhoto",
                    "body": {
                        "ok": True,
                        "result": {
                            "message_id": 102,
                            "date": 1700000000,
                            "chat": chat,
                            "photo": [
                                {
                                    "file_id": "corpus-photo-file",
                                    "file_unique_id": "corpus-photo-unique",
                                    "width": 1,
                                    "height": 1,
                                    "file_size": 70,
                                }
                            ],
                            "caption": "corpus telegram attachment",
                        },
                    },
                },
            ],
        ),
    ]


def _teams_specs() -> list[dict[str, Any]]:
    channel = {"name": "corpus-teams", "config": TEAMS_CONFIG}
    metadata = {"platform_destination": TEAMS_CONVERSATION, "service_url": TEAMS_SERVICE_URL}
    activity = {
        "type": "message",
        "id": "1700000000001",
        "timestamp": "2023-11-14T22:13:20.000Z",
        "serviceUrl": TEAMS_SERVICE_URL,
        "channelId": "msteams",
        "from": {"id": "29:corpus-user", "name": "Corpus User"},
        "conversation": {
            "id": TEAMS_CONVERSATION,
            "tenantId": "00000000-0000-4000-8000-000000000001",
        },
        "recipient": {"id": f"28:{TEAMS_APP_ID}", "name": "corpus bot"},
        "text": "corpus teams inbound",
    }
    card = {
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [{"type": "TextBlock", "text": "corpus teams attachment"}],
    }

    def reply(activity_id: str) -> dict[str, Any]:
        return {
            "host": "smba.trafficmanager.net",
            "method": "POST",
            "path": TEAMS_ACTIVITY_REPLY_PATH,
            "body": {"id": activity_id},
        }

    return [
        _spec(
            "teams_send",
            f"{ADAPTER_TESTS}/test_communications_adapters_teams.py::test_send_message_success",
            "https://learn.microsoft.com/en-us/azure/bot-service/rest-api/"
            "bot-framework-rest-connector-api-reference",
            {
                "channel": channel,
                "secrets": TEAMS_SECRETS,
                "call": "send_message",
                "message": {"content": "corpus teams send", "metadata": metadata},
            },
            [TEAMS_TOKEN, reply("1700000000101")],
        ),
        _spec(
            "teams_inbound",
            f"{ADAPTER_TESTS}/test_communications_adapters_teams.py::test_verify_webhook_success",
            "https://learn.microsoft.com/en-us/azure/bot-service/rest-api/"
            "bot-framework-rest-connector-authentication",
            {
                "channel": channel,
                "secrets": TEAMS_SECRETS,
                "call": "webhook",
                "teams_token": {
                    "kid": "corpus-wire-kid",
                    "claims": {
                        "iss": "https://api.botframework.com",
                        "aud": TEAMS_APP_ID,
                        "serviceUrl": TEAMS_SERVICE_URL,
                    },
                },
                "webhook": {
                    "body": json.dumps(activity, sort_keys=True),
                    "headers": {
                        "content-type": "application/json",
                        "authorization": f"Bearer {TEAMS_TOKEN_MARKER}",
                    },
                },
            },
            [
                TEAMS_TOKEN,
                {
                    "host": "login.botframework.com",
                    "method": "GET",
                    "path": "/v1/.well-known/keys",
                    "body": {
                        "keys": [
                            {
                                "kty": "RSA",
                                "use": "sig",
                                "kid": "corpus-wire-kid",
                                "alg": "RS256",
                                **TEAMS_KEY_MARKERS,
                            }
                        ]
                    },
                },
            ],
        ),
        _spec(
            "teams_attachment",
            f"{ADAPTER_TESTS}/test_communications_adapters_teams.py::test_send_message_adaptive_card",
            "https://learn.microsoft.com/en-us/azure/bot-service/rest-api/"
            "bot-framework-rest-connector-add-rich-cards",
            {
                "channel": channel,
                "secrets": TEAMS_SECRETS,
                "call": "send_message",
                "message": {
                    "content": json.dumps(card, sort_keys=True),
                    "content_type": "adaptive_card",
                    "metadata": metadata,
                },
            },
            [TEAMS_TOKEN, reply("1700000000102")],
        ),
    ]


def _email_specs() -> list[dict[str, Any]]:
    email_tests = f"{ADAPTER_TESTS}/test_communications_adapters_email.py::TestEmailAdapter"
    inbound = (
        "From: Corpus Sender <sender@corpus.test>\r\n"
        f"To: {EMAIL_ADDRESS}\r\n"
        "Subject: corpus email inbound\r\n"
        "Message-ID: <corpus-inbound-1@corpus.test>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "corpus email inbound\r\n"
    )
    smtp_channel = {"name": "corpus-email", "config": EMAIL_SMTP_CONFIG}
    imap_channel = {
        "name": "corpus-email",
        "config": {
            "imap_host": LOOPBACK,
            "imap_port": 993,
            "from_address": EMAIL_ADDRESS,
            "password": "$secret:EMAIL_PASSWORD",
        },
    }

    def mailbox(verb: str, body: str) -> dict[str, Any]:
        return {"host": LOOPBACK, "method": "IMAP", "path": verb, "body": body}

    return [
        _spec(
            "email_send",
            f"{email_tests}::test_send_message_success",
            "https://www.rfc-editor.org/rfc/rfc5321",
            {
                "channel": smtp_channel,
                "secrets": EMAIL_SECRETS,
                "mailbox": EMAIL_MAILBOX,
                "message_id": "<corpus-wire-1@corpus.test>",
                "call": "send_message",
                "message": {
                    "content": "corpus email send",
                    "metadata": {"subject": "corpus email send"},
                },
            },
            [EMAIL_SUBMISSION],
        ),
        # The Python adapter drops the fetched message: aioimaplib returns the RFC822 literal as
        # a bytearray and the adapter accepts only bytes, so it parses nothing and sends no
        # STORE. This fixture records that behaviour; it goes red, and is re-recorded, when the
        # adapter delivers the message and marks it seen.
        _spec(
            "email_inbound",
            f"{email_tests}::test_poll_with_messages",
            "https://www.rfc-editor.org/rfc/rfc3501",
            {
                "channel": imap_channel,
                "secrets": EMAIL_SECRETS,
                "mailbox": EMAIL_MAILBOX,
                "call": "poll",
            },
            [
                mailbox("SELECT", "* 1 EXISTS\n* 1 RECENT\n* FLAGS (\\Seen)"),
                mailbox("SEARCH", "* SEARCH 1"),
                mailbox("FETCH", inbound),
            ],
        ),
        _spec(
            "email_attachment",
            f"{email_tests}::test_send_attachment",
            "https://www.rfc-editor.org/rfc/rfc2183",
            {
                "channel": smtp_channel,
                "secrets": EMAIL_SECRETS,
                "mailbox": EMAIL_MAILBOX,
                "message_id": "<corpus-wire-2@corpus.test>",
                "call": "send_attachment",
                "message": {
                    "content": "corpus email attachment",
                    "metadata": {"subject": "corpus email attachment"},
                },
                "attachment": {
                    "filename": "corpus-report.txt",
                    "content_type": "text/plain",
                    "content_base64": TEXT_ATTACHMENT,
                },
            },
            [EMAIL_SUBMISSION],
        ),
    ]


def _sms_specs() -> list[dict[str, Any]]:
    channel = {"name": "corpus-sms", "config": SMS_CONFIG}
    destination = {"platform_destination": "+15550000002"}
    inbound = urlencode(
        {
            "AccountSid": SMS_ACCOUNT,
            "Body": "corpus sms inbound",
            "From": "+15550000002",
            "MessageSid": "SM00000000000000000000000000000002",
            "NumMedia": "0",
            "To": "+15550000001",
        }
    )
    return [
        _spec(
            "sms_send",
            f"{ADAPTER_TESTS}/test_communications_adapters_sms.py::test_send_message_success",
            "https://www.twilio.com/docs/messaging/api/message-resource#create-a-message-resource",
            {
                "channel": channel,
                "secrets": SMS_SECRETS,
                "call": "send_message",
                "message": {"content": "corpus sms send", "metadata": destination},
            },
            [
                {
                    "host": "api.twilio.com",
                    "method": "POST",
                    "path": SMS_MESSAGES_PATH,
                    "status": 201,
                    "body": {
                        "sid": "SM00000000000000000000000000000001",
                        "account_sid": SMS_ACCOUNT,
                        "to": "+15550000002",
                        "from": "+15550000001",
                        "body": "corpus sms send",
                        "status": "queued",
                        "direction": "outbound-api",
                    },
                }
            ],
        ),
        _spec(
            "sms_inbound",
            f"{ADAPTER_TESTS}/test_communications_adapters_sms.py::test_verify_webhook",
            "https://www.twilio.com/docs/usage/webhooks/webhooks-security",
            {
                "channel": channel,
                "secrets": SMS_SECRETS,
                "call": "webhook",
                "webhook": {
                    "body": inbound,
                    "headers": {
                        "content-type": "application/x-www-form-urlencoded",
                        "x-twilio-signature": _twilio_signature(inbound),
                    },
                },
            },
            [],
        ),
        _spec(
            "sms_attachment",
            "tests/communications/test_communications_attachments.py"
            "::test_sms_send_attachment_requires_url",
            "https://www.twilio.com/docs/messaging/api/message-resource#mediaurl",
            {
                "channel": channel,
                "secrets": SMS_SECRETS,
                "call": "send_attachment",
                "message": {"content": "corpus sms attachment", "metadata": destination},
                "attachment": {
                    "filename": "corpus-pixel.png",
                    "content_type": "image/png",
                    "content_base64": PNG_ATTACHMENT,
                    "platform_url": None,
                },
            },
            [],
        ),
    ]


def wire_specs() -> dict[str, dict[str, Any]]:
    """Every fixture's recording input, keyed by file stem."""
    specs = [
        *_slack_specs(),
        *_telegram_specs(),
        *_teams_specs(),
        *_email_specs(),
        *_sms_specs(),
    ]
    return {f"{spec['provider']}_{spec['operation']}": spec for spec in specs}
