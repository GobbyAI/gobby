"""Chunked transfer of derived transcript evidence across the process-pool boundary.

A long transcript derives thousands of frozen run and edit records. Unpickling them
in one ``pickle.loads`` rebuilds every record in C without running bytecode, so the
GIL is held until the last record exists and the daemon event loop stalls for tens
of milliseconds, far longer when the host preempts the holder. This codec moves
those records out of band into small chunks. The loop encodes and decodes one chunk
at a time and yields between chunks, so no single GIL hold outlasts one chunk.
"""

from __future__ import annotations

import asyncio
import io
import pickle
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from gobby.tasks.transcript_evidence_models import TranscriptEdit, TranscriptValidationRun

#: Records per chunk. A 256-run chunk decodes in a median of about 0.5 ms and a p99
#: of about 1.5 ms at load average 25-33 (2026-10-01, #23256). Under that contention
#: rare reconstruction spikes of 49-82 ms CPU (about 6 per 490,000 records) occur at
#: 64 and 256 records alike, so a smaller chunk would not bound them.
CHUNK_RECORDS = 256

_RECORD_TYPES = (TranscriptValidationRun, TranscriptEdit)


@dataclass(frozen=True)
class ChunkedPayload:
    """A pickled shell whose records travel separately in ordered chunks."""

    shell: bytes
    chunks: tuple[bytes, ...]
    record_count: int


class _RecordPickler(pickle.Pickler):
    def __init__(self, file: io.BytesIO) -> None:
        super().__init__(file, protocol=pickle.HIGHEST_PROTOCOL)
        self.records: list[object] = []
        self._indices: dict[int, int] = {}

    def persistent_id(self, obj: Any) -> int | None:
        if type(obj) not in _RECORD_TYPES:
            return None
        # Identity, not equality: a record referenced twice decodes as one object, as
        # pickle would preserve it; equal but distinct records stay distinct.
        index = self._indices.get(id(obj))
        if index is None:
            index = self._indices[id(obj)] = len(self.records)
            self.records.append(obj)
        return index


class _RecordUnpickler(pickle.Unpickler):
    def __init__(self, file: io.BytesIO, records: list[object]) -> None:
        super().__init__(file)
        self._records = records

    def persistent_load(self, pid: Any) -> object:
        if not isinstance(pid, int) or not 0 <= pid < len(self._records):
            raise pickle.UnpicklingError(f"invalid transcript record reference: {pid!r}")
        return self._records[pid]


def _encode_steps(value: object) -> Iterator[ChunkedPayload | None]:
    """Yield None after each chunk, then the finished payload."""
    shell = io.BytesIO()
    pickler = _RecordPickler(shell)
    pickler.dump(value)
    records = pickler.records
    chunks: list[bytes] = []
    for start in range(0, len(records), CHUNK_RECORDS):
        chunks.append(pickle.dumps(records[start : start + CHUNK_RECORDS], pickle.HIGHEST_PROTOCOL))
        yield None
    yield ChunkedPayload(shell=shell.getvalue(), chunks=tuple(chunks), record_count=len(records))


def _decode_steps(payload: ChunkedPayload) -> Iterator[object]:
    """Yield None after each chunk, then the decoded value; any invalid chunk raises."""
    records: list[object] = []
    for chunk in payload.chunks:
        decoded = pickle.loads(chunk)
        if not isinstance(decoded, list) or not all(type(r) in _RECORD_TYPES for r in decoded):
            raise pickle.UnpicklingError("invalid transcript record chunk")
        records.extend(decoded)
        yield None
    if len(records) != payload.record_count:
        raise pickle.UnpicklingError(
            f"transcript record chunks hold {len(records)} records, expected {payload.record_count}"
        )
    yield _RecordUnpickler(io.BytesIO(payload.shell), records).load()


def encode(value: object) -> ChunkedPayload:
    *_, payload = _encode_steps(value)
    assert payload is not None
    return payload


def decode(payload: ChunkedPayload) -> object:
    """Decode in one pass, for pool workers where no event loop waits."""
    *_, value = _decode_steps(payload)
    return value


async def encode_cooperatively(value: object) -> ChunkedPayload:
    """Encode on the event loop, yielding to it after each chunk."""
    for step in _encode_steps(value):
        if step is not None:
            return step
        await asyncio.sleep(0)
    raise AssertionError("unreachable: encoding always ends with a payload")


async def decode_cooperatively(payload: ChunkedPayload) -> object:
    """Decode on the event loop, yielding to it after each chunk.

    Nothing is returned until every chunk has decoded, so callers persist all or
    nothing, including when the decode is cancelled.
    """
    steps = _decode_steps(payload)
    for _ in payload.chunks:
        next(steps)
        await asyncio.sleep(0)
    return next(steps)


__all__ = [
    "CHUNK_RECORDS",
    "ChunkedPayload",
    "decode",
    "decode_cooperatively",
    "encode",
    "encode_cooperatively",
]
