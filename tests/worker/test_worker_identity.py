"""Tests for the process-wide schema identity cache that handshakes answer from."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from gobby.storage.schema_contract import expected_schema_identity
from gobby.worker import identity
from gobby.worker.facade import WorkerFacade

pytestmark = pytest.mark.unit

CREDENTIAL = "identity-test-credential"


@pytest.fixture(autouse=True)
def fresh_identity_cache() -> Iterator[None]:
    identity._cached_identity.cache_clear()
    yield
    identity._cached_identity.cache_clear()


def _handshake(generation: int) -> bytes:
    envelope = {
        "protocol_version": 1,
        "generation": generation,
        "operation_id": f"{generation}:1",
        "command": "handshake",
        "body": {"credential": CREDENTIAL},
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


async def test_handshake_reads_schema_identity_once(monkeypatch: pytest.MonkeyPatch) -> None:
    packaged_read = expected_schema_identity
    packaged = packaged_read()
    reads: list[int] = []

    def counting_read() -> dict[str, int | str]:
        reads.append(1)
        return packaged_read()

    monkeypatch.setattr("gobby.worker.identity.expected_schema_identity", counting_read)
    facade = WorkerFacade(credential=CREDENTIAL)
    first = json.loads(await facade.handle(_handshake(7)))
    second = json.loads(await facade.handle(_handshake(7)))
    assert first["result"]["schema_identity"] == packaged
    assert second["result"]["schema_identity"] == packaged
    assert len(reads) == 1


def test_failed_read_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    packaged = expected_schema_identity()
    calls: list[int] = []

    def flaky_read() -> dict[str, int | str]:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("packaged identity is briefly unreadable")
        return dict(packaged)

    monkeypatch.setattr("gobby.worker.identity.expected_schema_identity", flaky_read)
    with pytest.raises(OSError):
        identity.schema_identity()
    assert identity.schema_identity() == packaged
    assert len(calls) == 2
