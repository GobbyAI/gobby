"""Persistent sidecar store and bounded cache for transcript indexes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from collections import OrderedDict
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from gobby.paths import get_gobby_home
from gobby.sessions.message_stats import MessageStats
from gobby.sessions.transcripts.base import TokenUsage

if TYPE_CHECKING:
    from gobby.sessions.transcript_index import TranscriptIndex
    from gobby.sessions.transcripts.base import RawLine

logger = logging.getLogger("gobby.sessions.transcript_index")

#: Bounded LRU index cache size (entries). Each entry is tens of KB.
INDEX_CACHE_MAX_ENTRIES = 16
INDEX_SCHEMA_VERSION = 1
INDEX_SIDECAR_SUFFIX = ".gobby-index.json"
_SKIP_ADJUSTMENT_VALUE = object()

_IndexKey = tuple[str, str, str | None, str, int, int]

_INDEX_CACHE: OrderedDict[_IndexKey, TranscriptIndex] = OrderedDict()
_CACHE_LOCK = asyncio.Lock()
_BUILD_LOCKS: dict[_IndexKey, asyncio.Lock] = {}


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


def _index_to_payload(path: str, index: TranscriptIndex) -> dict[str, Any]:
    from gobby.sessions.transcript_index import _require_gzip_logical_size

    _require_gzip_logical_size(index.seek_mode, index.logical_size)
    source_stat = os.stat(path)
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "source_path": os.path.abspath(path),
        "source_device": source_stat.st_dev,
        "source_inode": source_stat.st_ino,
        "source_prefix_sha256": (
            _source_prefix_sha256(path, index.size) if index.seek_mode == "byte" else None
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
            for boundary in index.boundaries
        ],
        "parsed_boundaries": [
            {
                "raw_line_start": boundary.raw_line_start,
                "byte_start": boundary.byte_start,
                "parsed_index_start": boundary.parsed_index_start,
                "message_index_start": boundary.message_index_start,
                "role_counts_start": boundary.role_counts_start,
            }
            for boundary in index.parsed_boundaries
        ],
        "parsed_message_count": index.parsed_message_count,
        "raw_record_count": index.raw_record_count,
        "total_groups": index.total_groups,
        "tool_first_open": index.tool_first_open,
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
            for adjustment in index.post_pass_adjustments
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
    if stored_mtime_ns == mtime_ns and stored_size == size:
        return True
    if not allow_append or seek_mode != "byte":
        return False
    try:
        source_stat = os.stat(path)
    except OSError:
        return False
    if not 0 <= stored_size <= size:
        return False
    stored_prefix_digest = payload.get("source_prefix_sha256")
    if not isinstance(stored_prefix_digest, str):
        return False
    try:
        current_prefix_digest = _source_prefix_sha256(path, stored_size)
    except (OSError, ValueError):
        return False
    return (
        stored_mtime_ns <= mtime_ns
        and payload.get("source_device") == source_stat.st_dev
        and payload.get("source_inode") == source_stat.st_ino
        and stored_prefix_digest == current_prefix_digest
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
        with open(sidecar, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
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
        return index
    except (KeyError, TypeError, ValueError) as exc:
        logger.debug(
            "Invalid transcript index sidecar",
            extra={"sidecar_path": sidecar, "error": str(exc)},
        )
        return None


def persist_index_sidecar(path: str, index: TranscriptIndex) -> None:
    """Atomically persist an index sidecar in Gobby's cache."""
    sidecar = _sidecar_path(path)
    directory = os.path.dirname(sidecar)
    os.makedirs(directory, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{os.path.basename(sidecar)}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(_index_to_payload(path, index), handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, sidecar)
    except OSError as exc:
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
    _BUILD_LOCKS.clear()


def discard_index_sidecar(path: str) -> None:
    """Remove a transcript's persisted and in-memory index state."""
    absolute_path = os.path.abspath(path)
    try:
        os.unlink(_sidecar_path(path))
    except FileNotFoundError:
        pass

    for key in [key for key in _INDEX_CACHE if key[0] == absolute_path]:
        _INDEX_CACHE.pop(key, None)
    for key in [key for key in _BUILD_LOCKS if key[0] == absolute_path]:
        _BUILD_LOCKS.pop(key, None)


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
    """Return a cached index for the snapshot, building once off the event loop."""
    from gobby.sessions.transcript_index import (
        _require_gzip_logical_size,
        build_index_from_file,
        build_index_from_lines,
        build_index_from_raw_lines,
    )

    if lines is not None:
        seek_mode = "line"
        logical_size = None

    _require_gzip_logical_size(seek_mode, logical_size)
    key: _IndexKey = (os.path.abspath(path), source, session_id, seek_mode, mtime_ns, size)

    async with _CACHE_LOCK:
        cached = _INDEX_CACHE.get(key)
        if cached is not None:
            _INDEX_CACHE.move_to_end(key)
            return cached
        build_lock = _BUILD_LOCKS.setdefault(key, asyncio.Lock())

    async with build_lock:
        try:
            async with _CACHE_LOCK:
                cached = _INDEX_CACHE.get(key)
                if cached is not None:
                    _INDEX_CACHE.move_to_end(key)
                    return cached

            sidecar_index = await asyncio.to_thread(
                load_index_sidecar,
                path,
                source,
                session_id,
                seek_mode=seek_mode,
                mtime_ns=mtime_ns,
                size=size,
            )
            if sidecar_index is not None:
                async with _CACHE_LOCK:
                    _INDEX_CACHE[key] = sidecar_index
                    _INDEX_CACHE.move_to_end(key)
                    while len(_INDEX_CACHE) > INDEX_CACHE_MAX_ENTRIES:
                        _INDEX_CACHE.popitem(last=False)
                return sidecar_index

            if raw_lines is not None:
                index = await asyncio.to_thread(
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
                index = await asyncio.to_thread(
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
                index = await asyncio.to_thread(
                    build_index_from_file,
                    path,
                    source,
                    session_id,
                    mtime_ns=mtime_ns,
                    size=size,
                )
            await asyncio.to_thread(persist_index_sidecar, path, index)

            async with _CACHE_LOCK:
                _INDEX_CACHE[key] = index
                _INDEX_CACHE.move_to_end(key)
                while len(_INDEX_CACHE) > INDEX_CACHE_MAX_ENTRIES:
                    _INDEX_CACHE.popitem(last=False)
        finally:
            async with _CACHE_LOCK:
                _BUILD_LOCKS.pop(key, None)

    return index
