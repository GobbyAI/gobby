"""Typed gterm control-client failure behavior."""

from __future__ import annotations

import asyncio

import pytest

from gobby.terminals.host_client import (
    CommitTransportError,
    HostClient,
    HostCommandError,
    HostUnavailableError,
)

pytestmark = pytest.mark.unit


class _Writer:
    def __init__(self, *, write_error: Exception | None = None) -> None:
        self.write_error = write_error
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> object:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(data)
        return None

    async def drain(self) -> object:
        return None

    def close(self) -> object:
        return None

    async def wait_closed(self) -> object:
        return None


@pytest.mark.asyncio
async def test_commit_transport_error_reports_written_state() -> None:
    write_client = HostClient(
        asyncio.StreamReader(),
        _Writer(write_error=BrokenPipeError("write failed")),
    )
    with pytest.raises(CommitTransportError) as before_write:
        await write_client.spawn_commit("terminal-1", "spawn-1", 30_000)
    assert before_write.value.request_written is False

    reader = asyncio.StreamReader()
    reader.feed_eof()
    read_writer = _Writer()
    read_client = HostClient(reader, read_writer)
    with pytest.raises(CommitTransportError) as after_write:
        await read_client.spawn_commit("terminal-2", "spawn-2", 30_000)
    assert after_write.value.request_written is True
    assert b'"commit_deadline_ms":30000' in read_writer.writes[0]

    other_reader = asyncio.StreamReader()
    other_reader.feed_eof()
    with pytest.raises(HostUnavailableError):
        await HostClient(other_reader, _Writer()).ping()


def test_raise_for_payload_preserves_structured_error() -> None:
    with pytest.raises(HostCommandError) as raised:
        HostClient.raise_for_payload(
            {
                "ok": False,
                "error": "exec_failed",
                "code": "ENOENT",
                "detail": "No such file or directory",
                "stage": "exec",
            }
        )

    assert raised.value.error == "exec_failed"
    assert raised.value.code == "ENOENT"
    assert raised.value.detail == "No such file or directory"
    assert raised.value.stage == "exec"
