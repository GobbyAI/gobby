"""Persistent sidecar store and bounded cache for transcript indexes."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import tempfile
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from weakref import WeakValueDictionary

from gobby.paths import get_gobby_home
from gobby.sessions.message_stats import MessageStats
from gobby.sessions.transcript_search_timing import transcript_to_thread
from gobby.sessions.transcripts.base import TokenUsage

if TYPE_CHECKING:
    from gobby.sessions.transcript_index import TranscriptIndex
    from gobby.sessions.transcripts.base import RawLine

logger = logging.getLogger("gobby.sessions.transcript_index")

#: Bounded LRU index cache size (entries). Each entry is tens of KB.
INDEX_CACHE_MAX_ENTRIES = 16
# Version 3 adds bounded source verification and append journal generations.
# It also invalidates pre-fix AGY tool-event parser positions (version 1).
INDEX_SCHEMA_VERSION = 3
INDEX_SIDECAR_SUFFIX = ".gobby-index.json"
_SKIP_ADJUSTMENT_VALUE = object()

_IndexKey = tuple[str, str, str | None, str, int, int]

_INDEX_CACHE: OrderedDict[_IndexKey, TranscriptIndex] = OrderedDict()
_CACHE_LOCK = asyncio.Lock()
# Active and queued callers retain their lock; idle entries disappear automatically.
_BUILD_LOCKS: WeakValueDictionary[_IndexKey, asyncio.Lock] = WeakValueDictionary()
SOURCE_WINDOW_BYTES = 4096


@dataclass
class _PersistenceState:
    metadata: dict[str, Any]
    boundaries: int
    parsed_boundaries: int
    journal_size: int = 0
    tools: dict[str, int] = field(default_factory=dict)
    adjustments: list[Any] = field(default_factory=list)
    snapshot_metadata: dict[str, Any] | None = None


class IndexPersistenceOwner:
    """Private persistence slot, excluded from the consumer dataclass payload."""

    __slots__ = ("_sidecar_state", "_adjustment_positions")
    _sidecar_state: _PersistenceState
    _adjustment_positions: dict[tuple[int, str], int]

    def _set_sidecar_state(self, state: _PersistenceState) -> None:
        self._sidecar_state = state

    def _set_adjustment_positions(self, positions: dict[tuple[int, str], int]) -> None:
        self._adjustment_positions = positions


def adjustment_positions(index: TranscriptIndex) -> dict[tuple[int, str], int]:
    positions = getattr(index, "_adjustment_positions", None)
    if positions is None:
        positions = {
            (item.group_index, item.field): pos
            for pos, item in enumerate(index.post_pass_adjustments)
        }
        index._set_adjustment_positions(positions)
    return positions


def _persistence_state(index: TranscriptIndex) -> _PersistenceState | None:
    state = getattr(index, "_sidecar_state", None)
    return state if isinstance(state, _PersistenceState) else None


def clone_persistence_state(index: TranscriptIndex) -> None:
    """Detach the small pending journal delta when an appender is cloned."""
    state = _persistence_state(index)
    if state is not None:
        index._set_sidecar_state(
            replace(state, tools=dict(state.tools), adjustments=list(state.adjustments))
        )


def record_tool_open(index: TranscriptIndex, tool_id: str, position: int) -> None:
    if tool_id in index.tool_first_open:
        return
    index.tool_first_open[tool_id] = position
    state = _persistence_state(index)
    if state is not None:
        state.tools[tool_id] = position


def record_adjustments(index: TranscriptIndex, new: list[Any]) -> None:
    state = _persistence_state(index)
    if state is not None and new:
        keys = {(item.group_index, item.field) for item in new}
        state.adjustments = [
            item for item in state.adjustments if (item.group_index, item.field) not in keys
        ]
        positions = adjustment_positions(index)
        state.adjustments.extend(index.post_pass_adjustments[positions[key]] for key in keys)


def _source_windows(path: str, size: int) -> dict[str, Any]:
    st = os.stat(path)
    if st.st_size < size:
        raise ValueError("transcript shrank while indexing")
    width = min(size, SOURCE_WINDOW_BYTES)
    with open(path, "rb") as handle:
        head = handle.read(width)
        handle.seek(size - width)
        boundary = handle.read(width)
    return {
        "source_device": st.st_dev,
        "source_inode": st.st_ino,
        "source_head_sha256": hashlib.sha256(head).hexdigest(),
        "source_boundary_sha256": hashlib.sha256(boundary).hexdigest(),
    }


def _resident_matches(path: str, index: TranscriptIndex, *, mtime_ns: int, size: int) -> bool:
    state = _persistence_state(index)
    if state is None:
        return False
    try:
        return _sidecar_matches(
            state.snapshot_metadata or state.metadata,
            path=path,
            source=index.source,
            session_id=index.session_id,
            seek_mode=index.seek_mode,
            mtime_ns=mtime_ns,
            size=size,
            allow_append=True,
        )
    except (OSError, ValueError):
        return False


def refresh_source_snapshot(path: str, index: TranscriptIndex) -> None:
    """Verify/cache the parsed snapshot even when a journal write later fails."""
    state = _persistence_state(index)
    if state is None:
        return
    windows = _source_windows(path, index.size)
    if any(windows[key] != state.metadata[key] for key in ("source_device", "source_inode")):
        raise ValueError("transcript identity changed while extending index")
    state.snapshot_metadata = {
        **state.metadata,
        **windows,
        "mtime_ns": index.mtime_ns,
        "size": index.size,
    }


_CONTAINER_FIELDS = frozenset(
    {"boundaries", "parsed_boundaries", "tool_first_open", "post_pass_adjustments"}
)


@contextmanager
def _sidecar_lock(sidecar: str) -> Iterator[None]:
    os.makedirs(os.path.dirname(sidecar), exist_ok=True)
    with open(sidecar + ".lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _attach_state(index: TranscriptIndex, payload: dict[str, Any], journal_size: int) -> None:
    adjustment_positions(index)
    metadata = {key: value for key, value in payload.items() if key not in _CONTAINER_FIELDS}
    index._set_sidecar_state(
        _PersistenceState(
            metadata, len(index.boundaries), len(index.parsed_boundaries), journal_size
        )
    )


_CHECKPOINT_FIELDS = (
    "schema_version",
    "source_path",
    "source_device",
    "source_inode",
    "source_head_sha256",
    "source_boundary_sha256",
    "generation",
    "source",
    "session_id",
    "seek_mode",
    "mtime_ns",
    "size",
    "next_parser_index",
)


def _read_checkpoint(sidecar: str) -> dict[str, Any]:
    with open(sidecar + ".checkpoint", encoding="utf-8") as handle:
        if os.fstat(handle.fileno()).st_size > 16384:
            raise ValueError("oversized transcript index checkpoint")
        checkpoint = json.load(handle)
    if not isinstance(checkpoint, dict):
        raise ValueError("invalid transcript index checkpoint")
    return checkpoint


def _write_checkpoint(
    sidecar: str, payload: dict[str, Any], index: TranscriptIndex, journal_size: int
) -> None:
    checkpoint = {key: payload[key] for key in _CHECKPOINT_FIELDS}
    checkpoint.update(
        groups=len(index.boundaries),
        parsed_boundaries=len(index.parsed_boundaries),
        journal_size=journal_size,
    )
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=os.path.dirname(sidecar),
            prefix=".index-checkpoint-",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(checkpoint, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, sidecar + ".checkpoint")
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _replay_journal(sidecar: str, payload: dict[str, Any]) -> int:
    """Read history once on a cold load; writers never replay it on append."""
    adjustment_positions = {
        (item["group_index"], item["field"]): pos
        for pos, item in enumerate(payload["post_pass_adjustments"])
    }
    try:
        handle = open(sidecar + ".journal", "rb")
    except FileNotFoundError:
        return 0
    with handle:
        for line in handle:
            delta = json.loads(line)
            if (
                not line.endswith(b"\n")
                or delta["generation"] != payload["generation"]
                or delta["from_size"] != payload["size"]
                or delta["from_groups"] != len(payload["boundaries"])
                or delta["from_parsed_boundaries"] != len(payload["parsed_boundaries"])
            ):
                raise ValueError("non-contiguous transcript index journal")
            payload["boundaries"].extend(delta["boundaries"])
            payload["parsed_boundaries"].extend(delta["parsed_boundaries"])
            payload["tool_first_open"].update(delta["tool_first_open"])
            for adjustment in delta["post_pass_adjustments"]:
                key = (adjustment["group_index"], adjustment["field"])
                pos = adjustment_positions.get(key)
                if pos is None:
                    adjustment_positions[key] = len(payload["post_pass_adjustments"])
                    payload["post_pass_adjustments"].append(adjustment)
                else:
                    payload["post_pass_adjustments"][pos] = adjustment
            payload.update(
                {
                    key: value
                    for key, value in delta.items()
                    if key not in _CONTAINER_FIELDS and not key.startswith("from_")
                }
            )
        return handle.tell()


def _sidecar_path(path: str) -> str:
    normalized_path = os.path.abspath(path)
    cache_key = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()
    return str(
        get_gobby_home() / "cache" / "transcript-indexes" / f"{cache_key}{INDEX_SIDECAR_SUFFIX}"
    )


def _encode_adjustment_value(value: Any) -> Any:
    if isinstance(value, TokenUsage):
        return {
            "__type__": "TokenUsage",
            "input_tokens": value.input_tokens,
            "output_tokens": value.output_tokens,
            "cache_creation_tokens": value.cache_creation_tokens,
            "cache_read_tokens": value.cache_read_tokens,
        }
    if isinstance(value, bytes | bytearray | memoryview | set | frozenset):
        logger.debug(
            "Skipping non-serializable transcript index adjustment value",
            extra={
                "value_type": type(value).__name__,
                "value_length": len(value),
                "value_redacted": True,
            },
        )
        return _SKIP_ADJUSTMENT_VALUE
    try:
        json.dumps(value)
    except TypeError:
        try:
            value_length = len(value)
        except (TypeError, AttributeError):
            value_length = None
        logger.debug(
            "Skipping non-serializable transcript index adjustment value",
            extra={
                "value_type": type(value).__name__,
                "value_length": value_length,
                "value_redacted": True,
            },
        )
        return _SKIP_ADJUSTMENT_VALUE
    return value


def _decode_adjustment_value(value: Any) -> Any:
    if isinstance(value, dict) and value.get("__type__") == "TokenUsage":
        return TokenUsage(
            input_tokens=int(value.get("input_tokens", 0)),
            output_tokens=int(value.get("output_tokens", 0)),
            cache_creation_tokens=int(value.get("cache_creation_tokens", 0)),
            cache_read_tokens=int(value.get("cache_read_tokens", 0)),
        )
    return value


def _index_to_payload(
    path: str, index: TranscriptIndex, *, delta: _PersistenceState | None = None
) -> dict[str, Any]:
    from gobby.sessions.transcript_index import _require_gzip_logical_size

    _require_gzip_logical_size(index.seek_mode, index.logical_size)
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "source_path": os.path.abspath(path),
        **_source_windows(path, index.size),
        "generation": delta.metadata["generation"] if delta else uuid4().hex,
        "source_prefix_sha256": (
            _source_prefix_sha256(path, index.size)
            if index.seek_mode == "byte" and delta is None
            else None
        ),
        "source": index.source,
        "session_id": index.session_id,
        "seek_mode": index.seek_mode,
        "mtime_ns": index.mtime_ns,
        "size": index.size,
        "boundaries": [
            {
                "group_index": boundary.group_index,
                "raw_line_start": boundary.raw_line_start,
                "byte_start": boundary.byte_start,
                "parsed_index_start": boundary.parsed_index_start,
                "resume_safe": boundary.resume_safe,
                "role": boundary.role,
                "timestamp": boundary.timestamp.isoformat(),
            }
            for boundary in index.boundaries[delta.boundaries if delta else 0 :]
        ],
        "parsed_boundaries": [
            {
                "raw_line_start": boundary.raw_line_start,
                "byte_start": boundary.byte_start,
                "parsed_index_start": boundary.parsed_index_start,
                "message_index_start": boundary.message_index_start,
                "role_counts_start": boundary.role_counts_start,
            }
            for boundary in index.parsed_boundaries[delta.parsed_boundaries if delta else 0 :]
        ],
        "parsed_message_count": index.parsed_message_count,
        "raw_record_count": index.raw_record_count,
        "total_groups": index.total_groups,
        "tool_first_open": delta.tools if delta else index.tool_first_open,
        "role_message_counts": index.role_message_counts,
        "session_stats": dict(index.session_stats) if index.session_stats is not None else None,
        "next_parser_index": index.next_parser_index,
        "next_raw_line_no": index.next_raw_line_no,
        "safe_to_start_event": index.safe_to_start_event,
        "logical_size": index.logical_size,
        "parser_state": index.parser_state,
        "post_pass_adjustments": [
            {
                "group_index": adjustment.group_index,
                "field": adjustment.field,
                "value": encoded_value,
            }
            for adjustment in (delta.adjustments if delta else index.post_pass_adjustments)
            for encoded_value in [_encode_adjustment_value(adjustment.value)]
            if encoded_value is not _SKIP_ADJUSTMENT_VALUE
        ],
    }


def _source_prefix_sha256(path: str, size: int) -> str:
    """Hash exactly the indexed byte prefix of a transcript."""
    remaining = size
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while remaining:
            chunk = handle.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError(f"Transcript is shorter than indexed size {size}")
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _payload_to_index(payload: dict[str, Any]) -> TranscriptIndex:
    from gobby.sessions.transcript_index import (
        GroupBoundary,
        ParsedBoundary,
        RenderedAdjustment,
        TranscriptIndex,
    )

    raw_session_stats = payload.get("session_stats")
    session_stats: MessageStats | None = None
    if isinstance(raw_session_stats, dict):
        session_stats = MessageStats(
            message_count=int(raw_session_stats.get("message_count", 0)),
            turn_count=int(raw_session_stats.get("turn_count", 0)),
            tool_call_count=int(raw_session_stats.get("tool_call_count", 0)),
            last_assistant_content=(
                raw_session_stats["last_assistant_content"]
                if isinstance(raw_session_stats.get("last_assistant_content"), str)
                else None
            ),
        )
    next_parser_index = payload.get("next_parser_index", payload["parsed_message_count"])
    if next_parser_index is None:
        next_parser_index = payload["parsed_message_count"]
    next_raw_line_no = payload.get("next_raw_line_no", payload["raw_record_count"])
    if next_raw_line_no is None:
        next_raw_line_no = payload["raw_record_count"]
    safe_to_start_event = payload.get("safe_to_start_event")
    boundaries = [
        GroupBoundary(
            group_index=int(item["group_index"]),
            raw_line_start=int(item["raw_line_start"]),
            byte_start=item.get("byte_start"),
            parsed_index_start=int(item["parsed_index_start"]),
            resume_safe=bool(item["resume_safe"]),
            role=str(item["role"]),
            timestamp=datetime.fromisoformat(str(item["timestamp"])),
        )
        for item in payload.get("boundaries", [])
    ]
    parsed_boundaries = [
        ParsedBoundary(
            raw_line_start=int(item["raw_line_start"]),
            byte_start=item.get("byte_start"),
            parsed_index_start=int(item["parsed_index_start"]),
            message_index_start=int(item["message_index_start"]),
            role_counts_start={
                str(role): int(count)
                for role, count in dict(item.get("role_counts_start", {})).items()
            },
        )
        for item in payload.get("parsed_boundaries", [])
    ]
    adjustments = [
        RenderedAdjustment(
            group_index=int(item["group_index"]),
            field=str(item["field"]),
            value=_decode_adjustment_value(item.get("value")),
        )
        for item in payload.get("post_pass_adjustments", [])
    ]
    return TranscriptIndex(
        boundaries=boundaries,
        total_groups=int(payload["total_groups"]),
        parsed_message_count=int(payload["parsed_message_count"]),
        raw_record_count=int(payload["raw_record_count"]),
        source=str(payload["source"]),
        session_id=(payload["session_id"] if isinstance(payload.get("session_id"), str) else None),
        seek_mode=str(payload["seek_mode"]),
        mtime_ns=int(payload["mtime_ns"]),
        size=int(payload["size"]),
        tool_first_open={
            str(tool_id): int(index)
            for tool_id, index in payload.get("tool_first_open", {}).items()
        },
        post_pass_adjustments=adjustments,
        parsed_boundaries=parsed_boundaries,
        role_message_counts={
            str(role): int(count) for role, count in payload.get("role_message_counts", {}).items()
        },
        session_stats=session_stats,
        next_parser_index=int(next_parser_index),
        next_raw_line_no=int(next_raw_line_no),
        safe_to_start_event=(
            bool(safe_to_start_event) if safe_to_start_event is not None else True
        ),
        logical_size=(
            int(payload["logical_size"]) if payload.get("logical_size") is not None else None
        ),
        parser_state=(
            dict(payload["parser_state"]) if isinstance(payload.get("parser_state"), dict) else {}
        ),
    )


def _sidecar_matches(
    payload: dict[str, Any],
    *,
    path: str,
    source: str,
    session_id: str | None,
    seek_mode: str,
    mtime_ns: int,
    size: int,
    allow_append: bool = False,
) -> bool:
    identity_matches = (
        payload.get("schema_version") == INDEX_SCHEMA_VERSION
        and payload.get("source_path") == os.path.abspath(path)
        and payload.get("source") == source
        and payload.get("session_id") == session_id
        and payload.get("seek_mode") == seek_mode
    )
    if not identity_matches:
        return False

    stored_mtime_ns = int(payload.get("mtime_ns", -1))
    stored_size = int(payload.get("size", -1))
    exact = stored_mtime_ns == mtime_ns and stored_size == size
    if not exact and (not allow_append or seek_mode != "byte"):
        return False
    try:
        source_stat = os.stat(path)
    except OSError:
        return False
    if not 0 <= stored_size <= size:
        return False
    try:
        windows = _source_windows(path, stored_size)
    except (OSError, ValueError):
        return False
    return (
        stored_mtime_ns <= mtime_ns
        and payload.get("source_device") == source_stat.st_dev
        and payload.get("source_inode") == source_stat.st_ino
        and all(payload.get(key) == value for key, value in windows.items())
    )


def load_index_sidecar(
    path: str,
    source: str,
    session_id: str | None = None,
    *,
    seek_mode: str,
    mtime_ns: int,
    size: int,
    allow_append: bool = False,
) -> TranscriptIndex | None:
    """Load a matching sidecar, optionally accepting an append-only byte prefix."""
    sidecar = _sidecar_path(path)
    try:
        with _sidecar_lock(sidecar):
            with open(sidecar, encoding="utf-8") as handle:
                payload = json.load(handle)
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != INDEX_SCHEMA_VERSION
            ):
                return None
            checkpoint = _read_checkpoint(sidecar)
            if checkpoint["generation"] != payload.get("generation"):
                return None
            journal_size = _replay_journal(sidecar, payload)
            if (
                journal_size != checkpoint["journal_size"]
                or payload["size"] != checkpoint["size"]
                or len(payload["boundaries"]) != checkpoint["groups"]
                or len(payload["parsed_boundaries"]) != checkpoint["parsed_boundaries"]
            ):
                return None
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.debug(
            "Failed to read transcript index sidecar",
            extra={"sidecar_path": sidecar, "error": str(exc)},
        )
        return None

    try:
        if not isinstance(payload, dict) or not _sidecar_matches(
            payload,
            path=path,
            source=source,
            session_id=session_id,
            seek_mode=seek_mode,
            mtime_ns=mtime_ns,
            size=size,
            allow_append=allow_append,
        ):
            return None
        index = _payload_to_index(payload)
        if seek_mode == "gzip-block" and index.logical_size is None:
            return None
        _attach_state(index, payload, journal_size)
        return index
    except (KeyError, TypeError, ValueError) as exc:
        logger.debug(
            "Invalid transcript index sidecar",
            extra={"sidecar_path": sidecar, "error": str(exc)},
        )
        return None


def persist_index_sidecar(path: str, index: TranscriptIndex) -> None:
    """Persist a full build once, then append only the new index entries.

    Every call checks inode, shrink, head and prior-EOF windows. Same-size middle
    rewrites deliberately remain undetected until a full build; hashing the whole
    prefix on append would itself exceed the warm-path latency budget.
    """
    from gobby.sessions.transcript_index import _require_gzip_logical_size

    _require_gzip_logical_size(index.seek_mode, index.logical_size)
    sidecar = _sidecar_path(path)
    directory = os.path.dirname(sidecar)
    os.makedirs(directory, exist_ok=True)
    temp_name: str | None = None
    try:
        with _sidecar_lock(sidecar):
            state = _persistence_state(index)
            if state is not None:
                checkpoint = _read_checkpoint(sidecar)
                if checkpoint["generation"] != state.metadata["generation"]:
                    return
                groups = int(checkpoint["groups"])
                parsed = int(checkpoint["parsed_boundaries"])
                if not (
                    0 <= groups <= len(index.boundaries)
                    and 0 <= parsed <= len(index.parsed_boundaries)
                ):
                    return
                if not _sidecar_matches(
                    checkpoint,
                    path=path,
                    source=index.source,
                    session_id=index.session_id,
                    seek_mode=index.seek_mode,
                    mtime_ns=index.mtime_ns,
                    size=index.size,
                    allow_append=True,
                ):
                    return
                # Coordinate independent reader/processor cursors using only a
                # bounded checkpoint, never by loading the historical journal.
                delta = replace(
                    state,
                    metadata=checkpoint,
                    boundaries=groups,
                    parsed_boundaries=parsed,
                    journal_size=int(checkpoint["journal_size"]),
                    tools={
                        tool_id: position
                        for tool_id, position in state.tools.items()
                        if position >= (checkpoint.get("next_parser_index") or 0)
                    },
                )
                with open(sidecar + ".journal", "a+b") as handle:
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() < delta.journal_size:
                        return
                    handle.truncate(delta.journal_size)
                    handle.seek(delta.journal_size)
                    if (
                        checkpoint["size"] == index.size
                        and checkpoint["mtime_ns"] == index.mtime_ns
                        and not delta.tools
                        and not delta.adjustments
                    ):
                        _attach_state(index, checkpoint, delta.journal_size)
                        return
                    payload = _index_to_payload(path, index, delta=delta)
                    payload.update(
                        from_size=checkpoint["size"],
                        from_groups=groups,
                        from_parsed_boundaries=parsed,
                    )
                    handle.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
                    handle.flush()
                    os.fsync(handle.fileno())
                    _write_checkpoint(sidecar, payload, index, handle.tell())
                    _attach_state(index, payload, handle.tell())
                return
            payload = _index_to_payload(path, index)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=directory,
                prefix=f".{os.path.basename(sidecar)}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_name = handle.name
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, sidecar)
            temp_name = None
            with open(sidecar + ".journal", "wb") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            _write_checkpoint(sidecar, payload, index, 0)
            _attach_state(index, payload, 0)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.debug(
            "Failed to persist transcript index sidecar",
            extra={"sidecar_path": sidecar, "error": str(exc)},
        )
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def clear_index_cache() -> None:
    """Drop all cached indexes (invalidation / tests)."""
    _INDEX_CACHE.clear()


def discard_index_sidecar(path: str) -> None:
    """Remove a transcript's persisted and in-memory index state."""
    absolute_path = os.path.abspath(path)
    sidecar = _sidecar_path(path)
    with _sidecar_lock(sidecar):
        for suffix in ("", ".journal", ".checkpoint"):
            try:
                os.unlink(sidecar + suffix)
            except FileNotFoundError:
                pass
    # Keep the lock inode: replacing it could let two writers hold distinct locks.

    for key in [key for key in _INDEX_CACHE if key[0] == absolute_path]:
        _INDEX_CACHE.pop(key, None)


async def get_or_build_index(
    path: str,
    source: str,
    session_id: str | None,
    *,
    seek_mode: str = "byte",
    lines: Iterable[str] | None = None,
    raw_lines: Iterable[RawLine] | None = None,
    logical_size: int | None = None,
    mtime_ns: int,
    size: int,
) -> TranscriptIndex:
    """Finish and publish an index operation before propagating caller cancellation.

    Cancelling a to_thread await cannot stop its worker. Keep the entire operation
    alive so its lock covers shared mutations and its completed snapshot reaches
    the cache before another reader can extend the same resident containers.
    """
    operation = asyncio.create_task(
        _get_or_build_index(
            path,
            source,
            session_id,
            seek_mode=seek_mode,
            lines=lines,
            raw_lines=raw_lines,
            logical_size=logical_size,
            mtime_ns=mtime_ns,
            size=size,
        )
    )
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not operation.cancelled():
            operation.exception()
        raise


async def _get_or_build_index(
    path: str,
    source: str,
    session_id: str | None,
    *,
    seek_mode: str = "byte",
    lines: Iterable[str] | None = None,
    raw_lines: Iterable[RawLine] | None = None,
    logical_size: int | None = None,
    mtime_ns: int,
    size: int,
) -> TranscriptIndex:
    """Return a cached index for the snapshot, building once off the event loop."""
    from gobby.sessions.transcript_index import (
        _require_gzip_logical_size,
        build_index_from_file,
        build_index_from_lines,
        build_index_from_raw_lines,
    )
    from gobby.sessions.transcript_index_resume import extend_index_from_file

    if lines is not None:
        seek_mode = "line"
        logical_size = None

    _require_gzip_logical_size(seek_mode, logical_size)
    key: _IndexKey = (os.path.abspath(path), source, session_id, seek_mode, mtime_ns, size)
    lock_key: _IndexKey = (*key[:4], 0, 0)

    async with _CACHE_LOCK:
        build_lock = _BUILD_LOCKS.setdefault(lock_key, asyncio.Lock())

    async with build_lock:
        try:
            async with _CACHE_LOCK:
                cached = _INDEX_CACHE.get(key)
                prior = next(
                    (
                        value
                        for cached_key, value in reversed(_INDEX_CACHE.items())
                        if cached_key[:4] == key[:4]
                    ),
                    None,
                )
            if prior is not None and await transcript_to_thread(
                _resident_matches, path, prior, mtime_ns=mtime_ns, size=size
            ):
                if prior.mtime_ns == mtime_ns and prior.size == size:
                    return prior
                if seek_mode == "byte" and raw_lines is None and lines is None:
                    extended = await transcript_to_thread(
                        extend_index_from_file,
                        path,
                        source,
                        session_id,
                        mtime_ns=mtime_ns,
                        size=size,
                        prior=prior,
                    )
                    if extended is not None:
                        await transcript_to_thread(persist_index_sidecar, path, extended)
                        async with _CACHE_LOCK:
                            for old_key in list(_INDEX_CACHE):
                                if old_key[:4] == key[:4]:
                                    del _INDEX_CACHE[old_key]
                            _INDEX_CACHE[key] = extended
                        return extended
            elif (
                cached is not None
                and _persistence_state(cached) is None
                and (lines is not None or raw_lines is not None)
            ):
                # Non-file/lazy-line callers cannot attach source verification.
                return cached

            sidecar_index = await transcript_to_thread(
                load_index_sidecar,
                path,
                source,
                session_id,
                seek_mode=seek_mode,
                mtime_ns=mtime_ns,
                size=size,
                allow_append=seek_mode == "byte" and lines is None and raw_lines is None,
            )
            if sidecar_index is not None:
                if sidecar_index.mtime_ns != mtime_ns or sidecar_index.size != size:
                    sidecar_index = await transcript_to_thread(
                        extend_index_from_file,
                        path,
                        source,
                        session_id,
                        mtime_ns=mtime_ns,
                        size=size,
                        prior=sidecar_index,
                    )
                    if sidecar_index is not None:
                        await transcript_to_thread(persist_index_sidecar, path, sidecar_index)
            if sidecar_index is not None:
                async with _CACHE_LOCK:
                    _INDEX_CACHE[key] = sidecar_index
                    _INDEX_CACHE.move_to_end(key)
                    while len(_INDEX_CACHE) > INDEX_CACHE_MAX_ENTRIES:
                        _INDEX_CACHE.popitem(last=False)
                return sidecar_index

            if raw_lines is not None:
                index = await transcript_to_thread(
                    build_index_from_raw_lines,
                    raw_lines,
                    source,
                    session_id,
                    seek_mode=seek_mode,
                    mtime_ns=mtime_ns,
                    size=size,
                    transcript_path=path,
                    logical_size=logical_size,
                )
            elif lines is not None:
                index = await transcript_to_thread(
                    lambda: build_index_from_lines(
                        list(lines),
                        source,
                        session_id,
                        mtime_ns=mtime_ns,
                        size=size,
                        transcript_path=path,
                    )
                )
            else:
                index = await transcript_to_thread(
                    build_index_from_file, path, source, session_id, mtime_ns=mtime_ns, size=size
                )
            await transcript_to_thread(persist_index_sidecar, path, index)

            async with _CACHE_LOCK:
                _INDEX_CACHE[key] = index
                _INDEX_CACHE.move_to_end(key)
                while len(_INDEX_CACHE) > INDEX_CACHE_MAX_ENTRIES:
                    _INDEX_CACHE.popitem(last=False)
        finally:
            del build_lock

    return index
