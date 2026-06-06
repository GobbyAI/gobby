"""Blocked gzip archive indexing for random-access transcript windows."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import tempfile
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from gobby.sessions.transcript_io import DecompressionError
from gobby.sessions.transcripts.base import RawLine

GZIP_BLOCK_SEEK_MODE = "gzip-block"
GZIP_BLOCK_INDEX_SCHEMA_VERSION = 1
DEFAULT_GZIP_BLOCK_UNCOMPRESSED_SIZE = 8 * 1024 * 1024
_GZIP_BLOCK_CLEANUP_ERRORS = (
    DecompressionError,
    EOFError,
    gzip.BadGzipFile,
    zlib.error,
    OSError,
)


@dataclass(frozen=True, slots=True)
class GzipBlock:
    """One independently decompressible gzip member in a transcript archive."""

    compressed_start: int
    compressed_end: int
    uncompressed_start: int
    uncompressed_end: int
    raw_line_start: int
    raw_line_end: int


@dataclass(frozen=True, slots=True)
class GzipBlockIndex:
    """Sidecar metadata for a blocked gzip transcript archive."""

    source_path: str
    mtime_ns: int
    size: int
    block_size: int
    uncompressed_size: int
    raw_line_count: int
    blocks: tuple[GzipBlock, ...]


def gzip_block_sidecar_path(path: str) -> str:
    return f"{path}.gobby-gzip-index.json"


def write_blocked_gzip_archive(
    source_path: str,
    dest_path: str,
    *,
    block_size: int = DEFAULT_GZIP_BLOCK_UNCOMPRESSED_SIZE,
) -> GzipBlockIndex:
    """Compress a JSONL transcript as concatenated, independently seekable gzip members."""

    with open(source_path, "rb") as source:
        return write_blocked_gzip_from_lines(source, dest_path, block_size=block_size)


async def write_blocked_gzip_archive_async(
    source_path: str,
    dest_path: str,
    *,
    block_size: int = DEFAULT_GZIP_BLOCK_UNCOMPRESSED_SIZE,
) -> GzipBlockIndex:
    """Async wrapper for `write_blocked_gzip_archive` that offloads file I/O."""

    return await asyncio.to_thread(
        write_blocked_gzip_archive,
        source_path,
        dest_path,
        block_size=block_size,
    )


def write_blocked_gzip_from_lines(
    raw_lines: Iterable[bytes],
    dest_path: str,
    *,
    block_size: int = DEFAULT_GZIP_BLOCK_UNCOMPRESSED_SIZE,
) -> GzipBlockIndex:
    """Write raw JSONL bytes into a blocked gzip archive and persist its sidecar."""

    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(dest)

    try:
        index = _write_blocked_members(raw_lines, str(tmp), str(dest), block_size=block_size)
        os.replace(tmp, dest)
        st = os.stat(dest)
        index = replace(
            index, source_path=os.path.abspath(dest), mtime_ns=st.st_mtime_ns, size=st.st_size
        )
        persist_gzip_block_index(str(dest), index)
        return index
    except _GZIP_BLOCK_CLEANUP_ERRORS:
        _unlink_tmp(tmp)
        raise
    except BaseException:
        _unlink_tmp(tmp)
        raise


async def ensure_gzip_block_index(
    path: str,
    *,
    block_size: int = DEFAULT_GZIP_BLOCK_UNCOMPRESSED_SIZE,
    mtime_ns: int | None = None,
    size: int | None = None,
) -> GzipBlockIndex:
    """Return a valid block index, reblocking legacy single-member archives when needed."""

    return await asyncio.to_thread(
        _ensure_gzip_block_index_sync,
        path,
        block_size=block_size,
        mtime_ns=mtime_ns,
        size=size,
    )


def _ensure_gzip_block_index_sync(
    path: str,
    *,
    block_size: int = DEFAULT_GZIP_BLOCK_UNCOMPRESSED_SIZE,
    mtime_ns: int | None = None,
    size: int | None = None,
) -> GzipBlockIndex:
    """Synchronous implementation for the async public wrapper."""

    st = os.stat(path)
    index = load_gzip_block_index(
        path,
        mtime_ns=st.st_mtime_ns if mtime_ns is None else mtime_ns,
        size=st.st_size if size is None else size,
    )
    if index is not None and index.block_size == block_size:
        return index

    tmp = _tmp_path(Path(path))
    try:
        index = _write_blocked_members(
            _iter_gzip_raw_lines(path), str(tmp), path, block_size=block_size
        )
        os.replace(tmp, path)
        st = os.stat(path)
        index = replace(
            index, source_path=os.path.abspath(path), mtime_ns=st.st_mtime_ns, size=st.st_size
        )
        persist_gzip_block_index(path, index)
        return index
    except _GZIP_BLOCK_CLEANUP_ERRORS:
        _unlink_tmp(tmp)
        raise
    except BaseException:
        _unlink_tmp(tmp)
        raise


def load_gzip_block_index(
    path: str, *, mtime_ns: int | None = None, size: int | None = None
) -> GzipBlockIndex | None:
    """Load a block sidecar if it exactly matches the archive snapshot."""

    if mtime_ns is None or size is None:
        st = os.stat(path)
        mtime_ns = st.st_mtime_ns
        size = st.st_size

    sidecar = gzip_block_sidecar_path(path)
    try:
        with open(sidecar, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    try:
        if not _payload_matches(payload, path=path, mtime_ns=mtime_ns, size=size):
            return None
        return _payload_to_index(payload)
    except (KeyError, TypeError, ValueError):
        return None


def persist_gzip_block_index(path: str, index: GzipBlockIndex) -> None:
    sidecar = gzip_block_sidecar_path(path)
    payload = _index_to_payload(path, index)
    directory = os.path.dirname(sidecar) or "."
    fd, tmp = tempfile.mkstemp(prefix=".gobby-gzip-index-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        os.replace(tmp, sidecar)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def iter_gzip_block_raw_lines(
    path: str,
    index: GzipBlockIndex,
    start_byte: int,
    start_line_no: int,
) -> Iterator[RawLine]:
    """Yield positioned raw lines from the block containing ``start_byte``."""

    if not index.blocks:
        return

    block = _block_for_offset(index, start_byte)
    with open(path, "rb") as handle:
        handle.seek(block.compressed_start)
        try:
            with gzip.GzipFile(fileobj=handle, mode="rb") as archive:
                offset = block.uncompressed_start
                line_no = block.raw_line_start
                for raw_bytes in archive:
                    if offset >= index.uncompressed_size:
                        break
                    next_offset = offset + len(raw_bytes)
                    if line_no >= start_line_no and offset >= start_byte:
                        yield RawLine(
                            byte_offset=offset,
                            raw_line_no=line_no,
                            text=raw_bytes.decode("utf-8", errors="replace"),
                        )
                    offset = next_offset
                    line_no += 1
        except (EOFError, gzip.BadGzipFile, zlib.error) as exc:
            raise DecompressionError(f"Truncated or malformed gzip archive {path}: {exc}") from exc


def _tmp_path(dest: Path) -> Path:
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{dest.name}.",
        suffix=".tmp",
        dir=dest.parent,
        delete=False,
    )
    handle.close()
    return Path(handle.name)


def _unlink_tmp(tmp: Path) -> None:
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass


def _write_blocked_members(
    raw_lines: Iterable[bytes],
    dest_path: str,
    source_path: str,
    *,
    block_size: int,
) -> GzipBlockIndex:
    block_size = max(1, block_size)
    blocks: list[GzipBlock] = []
    total_uncompressed = 0
    total_lines = 0

    member: gzip.GzipFile | None = None
    block_compressed_start = 0
    block_uncompressed_start = 0
    block_raw_line_start = 0
    block_uncompressed_count = 0

    def open_block(handle: Any) -> None:
        nonlocal member, block_compressed_start, block_uncompressed_start
        nonlocal block_raw_line_start, block_uncompressed_count

        block_compressed_start = handle.tell()
        block_uncompressed_start = total_uncompressed
        block_raw_line_start = total_lines
        block_uncompressed_count = 0
        member = gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=0)

    def close_block(handle: Any) -> None:
        nonlocal member

        if member is None:
            return
        member.close()
        blocks.append(
            GzipBlock(
                compressed_start=block_compressed_start,
                compressed_end=handle.tell(),
                uncompressed_start=block_uncompressed_start,
                uncompressed_end=total_uncompressed,
                raw_line_start=block_raw_line_start,
                raw_line_end=total_lines,
            )
        )
        member = None

    def discard_open_member() -> None:
        nonlocal member

        if member is None:
            return
        try:
            member.close()
        except (OSError, RuntimeError, zlib.error):
            pass
        member = None

    try:
        with open(dest_path, "wb") as dest:
            try:
                for raw_line in raw_lines:
                    if member is None:
                        open_block(dest)
                    elif block_uncompressed_count >= block_size:
                        close_block(dest)
                        open_block(dest)

                    if member is None:
                        raise RuntimeError(
                            "open_block() returned with no gzip member; expected "
                            "member to be non-None before writing"
                        )
                    member.write(raw_line)
                    total_uncompressed += len(raw_line)
                    total_lines += 1
                    block_uncompressed_count += len(raw_line)

                if member is None:
                    with gzip.GzipFile(filename="", mode="wb", fileobj=dest, mtime=0):
                        pass
                else:
                    close_block(dest)
            except BaseException:
                discard_open_member()
                raise
    except (EOFError, gzip.BadGzipFile, zlib.error) as exc:
        raise DecompressionError(
            f"Truncated or malformed gzip archive {source_path}: {exc}"
        ) from exc

    st = os.stat(dest_path)
    return GzipBlockIndex(
        source_path=os.path.abspath(source_path),
        mtime_ns=st.st_mtime_ns,
        size=st.st_size,
        block_size=block_size,
        uncompressed_size=total_uncompressed,
        raw_line_count=total_lines,
        blocks=tuple(blocks),
    )


def _iter_gzip_raw_lines(path: str) -> Iterator[bytes]:
    try:
        with gzip.open(path, "rb") as archive:
            yield from archive
    except (EOFError, gzip.BadGzipFile, zlib.error) as exc:
        raise DecompressionError(f"Truncated or malformed gzip archive {path}: {exc}") from exc


def _block_for_offset(index: GzipBlockIndex, start_byte: int) -> GzipBlock:
    target = max(0, min(start_byte, max(0, index.uncompressed_size - 1)))
    blocks = index.blocks
    lo = 0
    hi = len(blocks)
    while lo < hi:
        mid = (lo + hi) // 2
        if blocks[mid].uncompressed_start <= target:
            lo = mid + 1
        else:
            hi = mid
    return blocks[max(0, lo - 1)]


def _payload_matches(payload: dict[str, Any], *, path: str, mtime_ns: int, size: int) -> bool:
    return (
        payload.get("schema_version") == GZIP_BLOCK_INDEX_SCHEMA_VERSION
        and payload.get("source_path") == os.path.abspath(path)
        and int(payload.get("mtime_ns", -1)) == mtime_ns
        and int(payload.get("size", -1)) == size
    )


def _index_to_payload(path: str, index: GzipBlockIndex) -> dict[str, Any]:
    return {
        "schema_version": GZIP_BLOCK_INDEX_SCHEMA_VERSION,
        "source_path": os.path.abspath(path),
        "mtime_ns": index.mtime_ns,
        "size": index.size,
        "block_size": index.block_size,
        "uncompressed_size": index.uncompressed_size,
        "raw_line_count": index.raw_line_count,
        "blocks": [
            {
                "compressed_start": block.compressed_start,
                "compressed_end": block.compressed_end,
                "uncompressed_start": block.uncompressed_start,
                "uncompressed_end": block.uncompressed_end,
                "raw_line_start": block.raw_line_start,
                "raw_line_end": block.raw_line_end,
            }
            for block in index.blocks
        ],
    }


def _payload_to_index(payload: dict[str, Any]) -> GzipBlockIndex:
    return GzipBlockIndex(
        source_path=str(payload["source_path"]),
        mtime_ns=int(payload["mtime_ns"]),
        size=int(payload["size"]),
        block_size=int(payload["block_size"]),
        uncompressed_size=int(payload["uncompressed_size"]),
        raw_line_count=int(payload["raw_line_count"]),
        blocks=tuple(
            GzipBlock(
                compressed_start=int(item["compressed_start"]),
                compressed_end=int(item["compressed_end"]),
                uncompressed_start=int(item["uncompressed_start"]),
                uncompressed_end=int(item["uncompressed_end"]),
                raw_line_start=int(item["raw_line_start"]),
                raw_line_end=int(item["raw_line_end"]),
            )
            for item in payload.get("blocks", [])
        ),
    )
