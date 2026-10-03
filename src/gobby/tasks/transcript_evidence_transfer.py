"""Chunked transfer of derived transcript evidence across the process-pool boundary.

A long transcript derives thousands of frozen run and edit records. Unpickling them
in one ``pickle.loads`` rebuilds every record in C without running bytecode, so the
GIL is held until the last record exists and the daemon event loop stalls for tens
of milliseconds, far longer when the host preempts the holder. This codec moves
every record sequence out of band into small chunks. The loop encodes and decodes
one chunk at a time and yields between chunks, so no single GIL hold outlasts one
chunk. The envelope left behind holds one reference per sequence, not per record, so
pickling it before the first chunk and unpickling it after the last stay bounded
at any transcript length (#23256). The only step that still scales with record
count is the final copy of each decoded list into its tuple, a C copy that runs
no Python code. Record identity is not preserved; decoded values are equal and in
order.
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
    """A pickled envelope whose record sequences travel separately in ordered chunks.

    Each chunk pickles ``(sequence index, records)``; the envelope refers to sequence
    ``index`` by the persistent id ``(index, is_tuple)``.
    """

    envelope: bytes
    chunks: tuple[bytes, ...]
    record_count: int


def _is_records(part: object) -> bool:
    return isinstance(part, list) and all(type(record) in _RECORD_TYPES for record in part)


class _SequencePickler(pickle.Pickler):
    def __init__(self, file: io.BytesIO) -> None:
        super().__init__(file, protocol=pickle.HIGHEST_PROTOCOL)
        self.sequences: list[tuple[object, ...] | list[object]] = []
        self._ids: dict[int, tuple[int, bool]] = {}

    def persistent_id(self, obj: Any) -> tuple[int, bool] | None:
        # The first element decides, in constant time; each chunk checks the rest.
        kind = type(obj)
        if (kind is not tuple and kind is not list) or not obj:
            return None
        if type(obj[0]) not in _RECORD_TYPES:
            return None
        pid = self._ids.get(id(obj))
        if pid is None:
            pid = self._ids[id(obj)] = (len(self.sequences), kind is tuple)
            self.sequences.append(obj)
        return pid


class _SequenceUnpickler(pickle.Unpickler):
    def __init__(self, file: io.BytesIO, sequences: dict[int, list[object]]) -> None:
        super().__init__(file)
        self._sequences = sequences
        self._loaded: dict[Any, object] = {}

    def persistent_load(self, pid: Any) -> object:
        loaded = self._loaded.get(pid)
        if loaded is not None:
            return loaded
        if not (
            isinstance(pid, tuple)
            and len(pid) == 2
            and isinstance(pid[1], bool)
            and pid[0] in self._sequences
        ):
            raise pickle.UnpicklingError(f"invalid transcript record reference: {pid!r}")
        records = self._sequences[pid[0]]
        loaded = self._loaded[pid] = tuple(records) if pid[1] else records
        return loaded


def _encode_steps(value: object) -> Iterator[ChunkedPayload | None]:
    """Yield None after each chunk, then the finished payload."""
    envelope = io.BytesIO()
    pickler = _SequencePickler(envelope)
    pickler.dump(value)
    chunks: list[bytes] = []
    record_count = 0
    for index, sequence in enumerate(pickler.sequences):
        for start in range(0, len(sequence), CHUNK_RECORDS):
            part = list(sequence[start : start + CHUNK_RECORDS])
            if not _is_records(part):
                raise TypeError("a transcript record sequence holds a non-record value")
            chunks.append(pickle.dumps((index, part), pickle.HIGHEST_PROTOCOL))
            record_count += len(part)
            yield None
    yield ChunkedPayload(
        envelope=envelope.getvalue(), chunks=tuple(chunks), record_count=record_count
    )


def _decode_steps(payload: ChunkedPayload) -> Iterator[object]:
    """Yield None after each chunk, then the decoded value; any invalid chunk raises."""
    sequences: dict[int, list[object]] = {}
    record_count = 0
    for chunk in payload.chunks:
        decoded = pickle.loads(chunk)
        if not (
            isinstance(decoded, tuple)
            and len(decoded) == 2
            and isinstance(decoded[0], int)
            and _is_records(decoded[1])
        ):
            raise pickle.UnpicklingError("invalid transcript record chunk")
        sequences.setdefault(decoded[0], []).extend(decoded[1])
        record_count += len(decoded[1])
        yield None
    if record_count != payload.record_count:
        raise pickle.UnpicklingError(
            f"transcript record chunks hold {record_count} records, expected {payload.record_count}"
        )
    yield _SequenceUnpickler(io.BytesIO(payload.envelope), sequences).load()


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
