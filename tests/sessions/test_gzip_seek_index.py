from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from gobby.sessions.gzip_seek_index import (
    ensure_gzip_block_index,
    gzip_block_sidecar_path,
    iter_gzip_block_raw_lines,
    load_gzip_block_index,
    write_blocked_gzip_archive,
)

pytestmark = pytest.mark.unit


def _write_source(tmp_path: Path, *, count: int = 16, width: int = 48) -> tuple[Path, bytes]:
    lines = [f'{{"i":{i},"text":"{"x" * width}{i}"}}\n'.encode() for i in range(count)]
    source = tmp_path / "transcript.jsonl"
    payload = b"".join(lines)
    source.write_bytes(payload)
    return source, payload


def test_write_blocked_gzip_archive_persists_seekable_sidecar(tmp_path: Path) -> None:
    source, payload = _write_source(tmp_path)
    archive = tmp_path / "ext.jsonl.gz"

    index = write_blocked_gzip_archive(str(source), str(archive), block_size=160)

    assert len(index.blocks) > 1
    assert gzip.decompress(archive.read_bytes()) == payload
    assert Path(gzip_block_sidecar_path(str(archive))).is_file()
    assert load_gzip_block_index(str(archive)) == index


def test_iter_gzip_block_raw_lines_starts_at_selected_block(tmp_path: Path) -> None:
    source, _payload = _write_source(tmp_path)
    archive = tmp_path / "ext.jsonl.gz"
    index = write_blocked_gzip_archive(str(source), str(archive), block_size=160)
    block = index.blocks[1]

    lines = list(
        iter_gzip_block_raw_lines(
            str(archive), index, block.uncompressed_start, block.raw_line_start
        )
    )

    assert lines[0].byte_offset == block.uncompressed_start
    assert lines[0].raw_line_no == block.raw_line_start
    assert f'"i":{block.raw_line_start}' in lines[0].text


def test_iter_gzip_block_raw_lines_ignores_prior_corrupt_block(tmp_path: Path) -> None:
    source, _payload = _write_source(tmp_path)
    archive = tmp_path / "ext.jsonl.gz"
    index = write_blocked_gzip_archive(str(source), str(archive), block_size=160)
    target = index.blocks[1]

    with archive.open("r+b") as handle:
        handle.seek(index.blocks[0].compressed_start + 10)
        handle.write(b"\x00" * 16)

    lines = list(
        iter_gzip_block_raw_lines(
            str(archive), index, target.uncompressed_start, target.raw_line_start
        )
    )

    assert lines[0].raw_line_no == target.raw_line_start
    assert f'"i":{target.raw_line_start}' in lines[0].text


def test_ensure_gzip_block_index_reblocks_legacy_single_member_archive(
    tmp_path: Path,
) -> None:
    source, payload = _write_source(tmp_path)
    archive = tmp_path / "legacy.jsonl.gz"
    with gzip.open(archive, "wb") as handle:
        handle.write(source.read_bytes())

    index = ensure_gzip_block_index(str(archive), block_size=160)

    assert len(index.blocks) > 1
    assert gzip.decompress(archive.read_bytes()) == payload
    assert load_gzip_block_index(str(archive)) == index
