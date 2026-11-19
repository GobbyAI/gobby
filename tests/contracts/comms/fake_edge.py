"""Loopback provider edge that stands in for the provider hosts the comms adapters reach.

The edge runs three loopback listeners. An HTTPS CONNECT proxy serves the provider hosts: it
terminates TLS with a test certificate authority, mints a leaf certificate for each CONNECT host on
demand, answers each request from the scripts in ``edge/`` and never forwards upstream. An IMAP
server with implicit TLS and an SMTP server with STARTTLS serve the email adapter's mailbox.

Every request through the proxy is logged, in arrival order, as host, method, path and its
sanitized ``semantic`` fields (``http_semantics``): the destination, the message text, the
attachment filename and the SHA-256 of the uploaded bytes. Headers and raw bodies are never
logged, so no request credential reaches the log. A request whose target the edge cannot parse,
whose body it cannot read or whose payload it cannot decode is still logged, with empty
semantic fields and a ``decode_error`` marker naming the stage and the error type, never the
error message. The CONNECT itself is not logged. A Telegram bot token in a path is replaced
before the log keeps the path. Any host or path no script names answers HTTP 200 with a
generic JSON body.
"""

from __future__ import annotations

import base64
import datetime as dt
import email
import email.policy
import hashlib
import ipaddress
import json
import re
import shlex
import socket
import ssl
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import parse_qsl, unquote, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

EDGE_DIR = Path(__file__).resolve().parent / "edge"
LOOPBACK = "127.0.0.1"
CA_FILE = "edge-ca.pem"
CA_NAME = "gobby corpus provider edge test authority"
LEAF_DIR = "leaves"
ACCEPT_POLL_SECONDS = 0.2
IO_TIMEOUT_SECONDS = 30.0
MAX_LINE_BYTES = 65536
TELEGRAM_BOT_PATH = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")
TELEGRAM_BOT_MARKER = "/bot@TELEGRAM_TOKEN@"
# The sanitized fields kept for each provider request, and the request fields they are read from.
SEMANTIC_FIELDS = ("destination", "text", "filename", "payload_sha256")
DECODE_ERROR_KEY = "decode_error"
DESTINATION_KEYS = ("channel", "channel_id", "chat_id", "To")
TEXT_KEYS = ("text", "initial_comment", "caption", "Body")
TEAMS_ACTIVITY_PATH = re.compile(r".*/v3/conversations/([^/]+)/activities")


@dataclass(frozen=True)
class _Reply:
    status: int
    content_type: str
    body: bytes


@dataclass(frozen=True)
class _Script:
    hosts: frozenset[str]
    default: _Reply
    routes: tuple[dict[str, Any], ...]
    mail: tuple[str, str] | None


UNSCRIPTED_REPLY = _Reply(200, "application/json", b'{"ok": true}')


def empty_semantic() -> dict[str, str]:
    """The semantic fields of a request that sends none of them."""
    return dict.fromkeys(SEMANTIC_FIELDS, "")


def payload_digest(payload: bytes | None) -> str:
    """The SHA-256 hex digest of uploaded bytes, or empty text when nothing was uploaded."""
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


def http_semantics(method: str, path: str, headers: dict[str, str], body: bytes) -> dict[str, str]:
    """The sanitized semantic fields of one HTTP provider request.

    ``headers`` are keyed by lowercase name. Only the content type is read from them, so no
    credential header reaches the result.
    """
    fields, files = _decode_http_body(headers.get("content-type", ""), body)
    semantic = empty_semantic()
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
    semantic["payload_sha256"] = payload_digest(payload)
    return semantic


def _decode_error(stage: str, error: BaseException) -> str:
    """A safe marker for a request the edge could not decode: the stage and the error type.

    The error message is never kept, because it can quote the raw body.
    """
    return f"{stage}: {type(error).__name__}"


def _encode_body(body: Any) -> bytes:
    if body is None:
        return b""
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(body, sort_keys=True).encode("utf-8")


def _reply_from(document: dict[str, Any], default: _Reply | None = None) -> _Reply:
    base = default or UNSCRIPTED_REPLY
    return _Reply(
        status=int(document.get("status", base.status)),
        content_type=str(document.get("content_type", base.content_type)),
        body=_encode_body(document["body"]) if "body" in document else base.body,
    )


def _script_from(document: dict[str, Any]) -> _Script:
    mail = document.get("mail")
    return _Script(
        hosts=frozenset(str(host) for host in document.get("hosts", [])),
        default=_reply_from(document.get("default", {})),
        routes=tuple(document.get("routes", [])),
        mail=(str(mail["user"]), str(mail["password"])) if isinstance(mail, dict) else None,
    )


def _load_scripts(directory: Path) -> list[_Script]:
    scripts: list[_Script] = []
    for path in sorted(directory.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        scripts.append(_script_from(document))
    return scripts


def _certificate(
    subject: x509.Name,
    public_key: ec.EllipticCurvePublicKey,
    issuer: x509.Name,
    signing_key: ec.EllipticCurvePrivateKey,
    *,
    ca: bool,
    sans: list[x509.GeneralName] | None,
) -> x509.Certificate:
    now = dt.datetime.now(dt.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    builder = builder.add_extension(
        x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False
    ).add_extension(
        x509.AuthorityKeyIdentifier.from_issuer_public_key(signing_key.public_key()),
        critical=False,
    )
    builder = builder.add_extension(
        x509.KeyUsage(
            digital_signature=True,
            content_commitment=False,
            key_encipherment=False,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=ca,
            crl_sign=ca,
            encipher_only=False,
            decipher_only=False,
        ),
        critical=True,
    )
    if not ca:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    return builder.sign(signing_key, hashes.SHA256())


def _general_name(name: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        return x509.DNSName(name)


class _Authority:
    """A test certificate authority for one edge, and the server contexts it signs for names."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._key = ec.generate_private_key(ec.SECP256R1())
        self._name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
        authority = _certificate(
            self._name, self._key.public_key(), self._name, self._key, ca=True, sans=None
        )
        self.ca_path = root / CA_FILE
        self.ca_path.write_bytes(authority.public_bytes(serialization.Encoding.PEM))
        self._contexts: dict[str, ssl.SSLContext] = {}
        self._lock = threading.Lock()

    def context(self, name: str) -> ssl.SSLContext:
        """A server context whose leaf certificate names ``name``, minted on first use."""
        with self._lock:
            cached = self._contexts.get(name)
            if cached is not None:
                return cached
            leaf_key = ec.generate_private_key(ec.SECP256R1())
            leaf = _certificate(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]),
                leaf_key.public_key(),
                self._name,
                self._key,
                ca=False,
                sans=[_general_name(name)],
            )
            directory = self.ca_path.parent / LEAF_DIR
            directory.mkdir(exist_ok=True)
            stem = re.sub(r"[^A-Za-z0-9.-]", "_", name)
            cert_file = directory / f"{stem}.crt"
            key_file = directory / f"{stem}.key"
            cert_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
            key_file.write_bytes(
                leaf_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=cert_file, keyfile=key_file)
            self._contexts[name] = context
            return context


def _read_head(reader: BinaryIO) -> tuple[str, dict[str, str]] | None:
    """One request line and its headers, with names lowercased; None at end of stream."""
    first = reader.readline(MAX_LINE_BYTES)
    if not first:
        return None
    headers: dict[str, str] = {}
    while True:
        line = reader.readline(MAX_LINE_BYTES)
        if line in (b"", b"\r\n", b"\n"):
            break
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    return first.decode("latin-1").rstrip("\r\n"), headers


def _read_body(reader: BinaryIO, headers: dict[str, str]) -> bytes:
    if headers.get("transfer-encoding", "").lower() == "chunked":
        chunks: list[bytes] = []
        while True:
            size_line = reader.readline(MAX_LINE_BYTES)
            size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                while reader.readline(MAX_LINE_BYTES) not in (b"", b"\r\n", b"\n"):
                    pass
                return b"".join(chunks)
            chunks.append(reader.read(size))
            reader.readline(MAX_LINE_BYTES)
    length = int(headers.get("content-length", "0") or "0")
    return reader.read(length) if length else b""


def _write_reply(sink: socket.socket | ssl.SSLSocket, reply: _Reply) -> None:
    phrase = HTTPStatus(reply.status).phrase
    head = (
        f"HTTP/1.1 {reply.status} {phrase}\r\n"
        f"Content-Type: {reply.content_type}\r\n"
        f"Content-Length: {len(reply.body)}\r\n"
        "Connection: keep-alive\r\n\r\n"
    )
    sink.sendall(head.encode("latin-1") + reply.body)


def _imap_reply(tag: str, command: str, arguments: str, mail: tuple[str, str] | None) -> bytes:
    """The IMAP answer to one tagged command; LOGOUT is handled by the caller."""
    if command == "CAPABILITY":
        return (
            f"* CAPABILITY IMAP4rev1 AUTH=PLAIN LOGIN\r\n{tag} OK CAPABILITY completed\r\n"
        ).encode()
    if command == "NOOP":
        return f"{tag} OK NOOP completed\r\n".encode()
    if command == "LOGIN":
        try:
            parts = shlex.split(arguments)
        except ValueError:
            return f"{tag} BAD LOGIN arguments are not quoted correctly\r\n".encode()
        if mail is not None and parts == list(mail):
            return f"{tag} OK LOGIN completed\r\n".encode()
        return f"{tag} NO LOGIN failed\r\n".encode()
    return f"{tag} BAD command not supported\r\n".encode()


def _smtp_greeting(secure: bool) -> bytes:
    if secure:
        return b"250-corpus\r\n250 AUTH PLAIN LOGIN\r\n"
    return b"250-corpus\r\n250 STARTTLS\r\n"


class FakeEdge:
    """A loopback provider edge, owned by one recorder run or one test."""

    def __init__(self, root: Path) -> None:
        """Keep the test authority and its key under ``root``. Nothing is bound until ``start``."""
        self._root = root
        self._authority: _Authority | None = None
        self._scripts: list[_Script] = []
        self._listeners: dict[str, socket.socket] = {}
        self._addresses: dict[str, tuple[str, int]] = {}
        self._hits: list[dict[str, Any]] = []
        self._hits_lock = threading.Lock()
        self._closing = False

    def start(self) -> None:
        """Create the test authority and bind the loopback listeners."""
        if self._authority is not None:
            raise RuntimeError("the fake edge is already started")
        self._scripts = _load_scripts(EDGE_DIR)
        self._authority = _Authority(self._root)
        handlers: dict[str, Callable[[socket.socket], None]] = {
            "proxy": self._serve_proxy_client,
            "imap": self._serve_imap_client,
            "smtp": self._serve_smtp_client,
        }
        for name, handler in handlers.items():
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind((LOOPBACK, 0))
            listener.listen(16)
            listener.settimeout(ACCEPT_POLL_SECONDS)
            self._listeners[name] = listener
            self._addresses[name] = (LOOPBACK, int(listener.getsockname()[1]))
            threading.Thread(
                target=self._accept_forever, args=(listener, handler), daemon=True
            ).start()

    def stop(self) -> None:
        """Close every listener."""
        self._closing = True
        for listener in self._listeners.values():
            listener.close()
        self._listeners.clear()

    @property
    def address(self) -> tuple[str, int]:
        """The bound HTTPS CONNECT proxy address, as (host, port)."""
        return self._addresses.get("proxy", ("", 0))

    def listener(self, protocol: str) -> tuple[str, int]:
        """The bound address of an email listener, ``imap`` or ``smtp``, as (host, port)."""
        if protocol not in ("imap", "smtp"):
            raise ValueError(f"the fake edge has no {protocol!r} listener")
        return self._addresses.get(protocol, ("", 0))

    def ca_path(self) -> Path:
        """The PEM file of the test authority that clients must trust."""
        return self._root / CA_FILE

    def hits(self) -> list[dict[str, Any]]:
        """Every arrival since the last clear, in arrival order: host, method, path, semantic.

        An arrival the edge could not decode also carries its ``DECODE_ERROR_KEY`` marker.
        """
        with self._hits_lock:
            return [{**hit, "semantic": dict(hit["semantic"])} for hit in self._hits]

    def clear(self) -> None:
        """Forget every arrival so far."""
        with self._hits_lock:
            self._hits.clear()

    def _accept_forever(
        self, listener: socket.socket, handler: Callable[[socket.socket], None]
    ) -> None:
        while not self._closing:
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.settimeout(IO_TIMEOUT_SECONDS)
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    def _reply_for(self, host: str, method: str, path: str) -> _Reply:
        for script in self._scripts:
            if host not in script.hosts:
                continue
            for route in script.routes:
                if (
                    route.get("host", host) == host
                    and route.get("method") == method
                    and route.get("path") == path
                ):
                    return _reply_from(route, script.default)
            return script.default
        return UNSCRIPTED_REPLY

    def _log(
        self,
        host: str,
        method: str,
        path: str,
        semantic: dict[str, str],
        decode_error: str | None = None,
    ) -> None:
        logged = TELEGRAM_BOT_PATH.sub(TELEGRAM_BOT_MARKER, path)
        hit: dict[str, Any] = {
            "host": host,
            "method": method,
            "path": logged,
            "semantic": dict(semantic),
        }
        if decode_error is not None:
            hit[DECODE_ERROR_KEY] = decode_error
        with self._hits_lock:
            self._hits.append(hit)

    def _serve_proxy_client(self, conn: socket.socket) -> None:
        assert self._authority is not None
        with conn:
            reader = conn.makefile("rb")
            head = _read_head(reader)
            reader.close()
            if head is None:
                return
            request_line, _ = head
            method, _, target = request_line.partition(" ")
            if method != "CONNECT":
                conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                return
            host = target.rpartition(":")[0].strip("[]")
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            try:
                tls = self._authority.context(host).wrap_socket(conn, server_side=True)
            except (OSError, ssl.SSLError):
                return
            with tls:
                self._serve_tunnel(tls, host)

    def _serve_tunnel(self, tls: ssl.SSLSocket, host: str) -> None:
        reader = tls.makefile("rb")
        try:
            while self._serve_request(tls, reader, host):
                pass
        except (OSError, ssl.SSLError, ValueError):
            return
        finally:
            reader.close()

    def _serve_request(self, tls: ssl.SSLSocket, reader: BinaryIO, host: str) -> bool:
        """Answer one tunnelled request; False when the tunnel closes after it.

        Every request whose head arrives is logged exactly once, in the ``finally`` that every
        exit passes through. A failure at any stage (target parse, body read, payload decode)
        leaves empty semantic fields and a marker naming that stage and the error type.
        """
        head = _read_head(reader)
        if head is None:
            return False
        request_line, headers = head
        method, _, rest = request_line.partition(" ")
        target = rest.rpartition(" ")[0] or rest
        # The target with no query, logged when the target itself cannot be parsed.
        path = target.partition("?")[0] or "/"
        semantic = empty_semantic()
        stage = "target"
        failure: Exception | None = None
        try:
            path = urlsplit(target).path or "/"
            stage = "body"
            body = _read_body(reader, headers)
            stage = "payload"
            semantic = http_semantics(method, path, headers, body)
        except Exception as error:  # every failure is logged by stage; see the finally
            failure = error
        finally:
            raised = failure or sys.exception()
            if raised is None:
                self._log(host, method, path, semantic)
            else:
                self._log(host, method, path, empty_semantic(), _decode_error(stage, raised))
        if failure is not None and stage != "payload":
            # The framing is lost, so the tunnel closes after the logged arrival.
            return False
        _write_reply(tls, self._reply_for(host, method, path))
        return headers.get("connection", "").lower() != "close"

    def _mail_credentials(self) -> tuple[str, str] | None:
        for script in self._scripts:
            if script.mail is not None:
                return script.mail
        return None

    def _serve_imap_client(self, conn: socket.socket) -> None:
        assert self._authority is not None
        with conn:
            try:
                tls = self._authority.context(LOOPBACK).wrap_socket(conn, server_side=True)
            except (OSError, ssl.SSLError):
                return
            with tls:
                tls.sendall(b"* OK [CAPABILITY IMAP4rev1 AUTH=PLAIN LOGIN] corpus IMAP ready\r\n")
                reader = tls.makefile("rb")
                try:
                    self._imap_session(tls, reader)
                except (OSError, ssl.SSLError, ValueError):
                    return
                finally:
                    reader.close()

    def _imap_session(self, tls: ssl.SSLSocket, reader: BinaryIO) -> None:
        mail = self._mail_credentials()
        while True:
            line = reader.readline(MAX_LINE_BYTES)
            if not line:
                return
            tag, _, rest = line.decode("utf-8", "replace").rstrip("\r\n").partition(" ")
            command, _, arguments = rest.partition(" ")
            if command.upper() == "LOGOUT":
                tls.sendall(f"* BYE corpus IMAP closing\r\n{tag} OK LOGOUT completed\r\n".encode())
                return
            tls.sendall(_imap_reply(tag, command.upper(), arguments, mail))

    def _serve_smtp_client(self, conn: socket.socket) -> None:
        assert self._authority is not None
        with conn:
            conn.sendall(b"220 corpus SMTP ready\r\n")
            session = _SmtpSession(
                conn, self._authority.context(LOOPBACK), self._mail_credentials()
            )
            session.run()


class _SmtpSession:
    """One SMTP connection: plain EHLO, STARTTLS, then the AUTH the adapter uses."""

    def __init__(
        self,
        conn: socket.socket,
        context: ssl.SSLContext,
        mail: tuple[str, str] | None,
    ) -> None:
        self._conn = conn
        self._context = context
        self._mail = mail
        self._sock: socket.socket | ssl.SSLSocket = conn
        self._secure = False

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
                elif verb in ("NOOP", "RSET"):
                    self._sock.sendall(b"250 OK\r\n")
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

    def _auth(self, reader: BinaryIO, arguments: str) -> None:
        mechanism, _, initial = arguments.partition(" ")
        if not self._secure:
            self._sock.sendall(b"530 Must issue a STARTTLS command first\r\n")
            return
        if mechanism.upper() != "PLAIN":
            self._sock.sendall(b"504 Unrecognized authentication type\r\n")
            return
        if not initial:
            self._sock.sendall(b"334 \r\n")
            initial = reader.readline(MAX_LINE_BYTES).decode("utf-8", "replace").strip()
        try:
            payload = base64.b64decode(initial, validate=True).split(b"\x00")
        except ValueError:
            self._sock.sendall(b"501 Malformed authentication response\r\n")
            return
        user, password = (payload[-2], payload[-1]) if len(payload) >= 3 else (b"", b"")
        expected = self._mail
        if expected is not None and (user, password) == (
            expected[0].encode("utf-8"),
            expected[1].encode("utf-8"),
        ):
            self._sock.sendall(b"235 2.7.0 Authentication successful\r\n")
        else:
            self._sock.sendall(b"535 5.7.8 Authentication credentials invalid\r\n")
