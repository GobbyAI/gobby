"""Tests for the worker channel: the marked-start boot, the framed reply and the golden replay.

A marked start reads one inherited credential descriptor and one channel descriptor, both
named by environment variables that only the Rust supervisor sets. The golden replay checks
the handshake vectors shared with the Rust client.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import struct
from collections.abc import Callable
from pathlib import Path

import pytest

from gobby.runner_init import StartRefusal
from gobby.storage.schema_contract import expected_schema_identity
from gobby.worker.channel import (
    WORKER_CHANNEL_FD_ENV,
    WORKER_CREDENTIAL_FD_ENV,
    WORKER_MARKER_ENV,
    WorkerBoot,
    serve_worker,
    take_worker_boot,
)
from gobby.worker.protocol import ACCEPTED_PROTOCOL_VERSIONS

pytestmark = pytest.mark.unit

CREDENTIAL = "channel-test-credential"
SECRET = "never-echoed-credential"
GOLDEN_PATH = Path(__file__).resolve().parents[1] / "contracts" / "worker_handshake.json"
SCHEMA_IDENTITY_PLACEHOLDER = json.dumps("$schema_identity")
MIN_WIRE_INTEGER = -(2**63)
MAX_WIRE_INTEGER = 2**64 - 1
REPLY_TIMEOUT_SECONDS = 10.0
FRAME = struct.Struct(">I")
BOOT_VARIABLES = (WORKER_MARKER_ENV, WORKER_CHANNEL_FD_ENV, WORKER_CREDENTIAL_FD_ENV)


def _credential_descriptor(payload: bytes) -> int:
    """Return the read end of a pipe holding payload, with its write end already closed."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, payload)
    os.close(write_fd)
    return read_fd


def _close_quietly(fd: int) -> None:
    """Close a descriptor that the code under test may already have closed."""
    with contextlib.suppress(OSError):
        os.close(fd)


def test_take_worker_boot_pops_marker_and_reads_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    credential_fd = _credential_descriptor(CREDENTIAL.encode("utf-8"))
    channel_socket, peer = socket.socketpair()
    try:
        monkeypatch.setenv(WORKER_MARKER_ENV, "1")
        monkeypatch.setenv(WORKER_CHANNEL_FD_ENV, str(channel_socket.fileno()))
        monkeypatch.setenv(WORKER_CREDENTIAL_FD_ENV, str(credential_fd))
        boot = take_worker_boot()
        assert boot == WorkerBoot(channel_fd=channel_socket.fileno(), credential=CREDENTIAL)
        assert not any(name in os.environ for name in BOOT_VARIABLES)
        with pytest.raises(OSError):
            os.fstat(credential_fd)
    finally:
        _close_quietly(credential_fd)
        channel_socket.close()
        peer.close()


@pytest.mark.parametrize("channel_value", [None, "three"], ids=["missing", "not-an-integer"])
def test_take_worker_boot_refuses_a_marker_without_descriptors(
    monkeypatch: pytest.MonkeyPatch, channel_value: str | None
) -> None:
    credential_fd = _credential_descriptor(b"")
    try:
        monkeypatch.setenv(WORKER_MARKER_ENV, "1")
        monkeypatch.setenv(WORKER_CREDENTIAL_FD_ENV, str(credential_fd))
        if channel_value is None:
            monkeypatch.delenv(WORKER_CHANNEL_FD_ENV, raising=False)
        else:
            monkeypatch.setenv(WORKER_CHANNEL_FD_ENV, channel_value)
        with pytest.raises(StartRefusal) as refused:
            take_worker_boot()
        assert WORKER_CHANNEL_FD_ENV in str(refused.value)
    finally:
        _close_quietly(credential_fd)


def test_take_worker_boot_refuses_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    credential_fd = _credential_descriptor(b"")
    channel_socket, peer = socket.socketpair()
    try:
        monkeypatch.setenv(WORKER_MARKER_ENV, "1")
        monkeypatch.setenv(WORKER_CHANNEL_FD_ENV, str(channel_socket.fileno()))
        monkeypatch.setenv(WORKER_CREDENTIAL_FD_ENV, str(credential_fd))
        monkeypatch.setattr(os, "name", "nt")
        with pytest.raises(StartRefusal):
            take_worker_boot()
    finally:
        _close_quietly(credential_fd)
        channel_socket.close()
        peer.close()


def test_take_worker_boot_absent_marker_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in BOOT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    assert take_worker_boot() is None


def test_worker_boot_repr_omits_credential() -> None:
    boot = WorkerBoot(channel_fd=3, credential=SECRET)
    assert SECRET not in repr(boot)


def _frame(payload: bytes) -> bytes:
    return FRAME.pack(len(payload)) + payload


def _handshake_request(credential: str) -> bytes:
    envelope = {
        "protocol_version": 1,
        "generation": 7,
        "operation_id": "7:1",
        "command": "handshake",
        "body": {"credential": credential},
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


async def _send_and_read_reply(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request: bytes
) -> bytes | None:
    """Send one request frame and return the reply frame, or None if the channel closes first."""
    try:
        async with asyncio.timeout(REPLY_TIMEOUT_SECONDS):
            writer.write(_frame(request))
            await writer.drain()
            (size,) = FRAME.unpack(await reader.readexactly(FRAME.size))
            return await reader.readexactly(size)
    except (ConnectionError, asyncio.IncompleteReadError, TimeoutError):
        return None


async def test_serve_worker_answers_a_handshake_over_the_channel() -> None:
    parent, child = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=parent)
    serve = asyncio.create_task(
        serve_worker(WorkerBoot(channel_fd=child.detach(), credential=CREDENTIAL))
    )
    try:
        reply = await _send_and_read_reply(reader, writer, _handshake_request(CREDENTIAL))
    finally:
        writer.close()
        async with asyncio.timeout(REPLY_TIMEOUT_SECONDS):
            await serve
    assert reply is not None, "the channel closed before a reply frame arrived"
    envelope = json.loads(reply)
    assert envelope["result"]["protocol_version"] == 1


def _recording_integers(values: list[int]) -> Callable[[str], int]:
    """A json parse_int hook that records every integer literal it reads."""

    def parse(text: str) -> int:
        value = int(text)
        values.append(value)
        return value

    return parse


def test_golden_vectors_replay_python() -> None:
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert golden["schema"] == "worker-handshake-golden"
    assert golden["versions"] == list(ACCEPTED_PROTOCOL_VERSIONS)
    identity = expected_schema_identity()
    identity_text = json.dumps(identity, sort_keys=True)
    for case in golden["cases"]:
        name = case["name"]
        text = json.dumps(case["reply"]).replace(SCHEMA_IDENTITY_PLACEHOLDER, identity_text)
        integers: list[int] = []
        reply = json.loads(text, parse_int=_recording_integers(integers))
        result = reply["result"]
        out_of_range = [
            value for value in integers if not MIN_WIRE_INTEGER <= value <= MAX_WIRE_INTEGER
        ]
        if case["expect"] == "unknown_outcome":
            assert out_of_range, name
        elif case["expect"] == "identity_mismatch":
            assert case["command"] == "handshake", name
            assert result["schema_identity"] != identity, name
        else:
            assert case["expect"] == "accepted", name
            assert not out_of_range, name
            if case["command"] == "handshake":
                assert result["schema_identity"] == identity, name
