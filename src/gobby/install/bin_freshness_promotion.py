"""Staged, atomic promotion helpers for managed native binaries."""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import IO

from gobby.install.bin_freshness_github import SourceUnavailableError

NATIVE_BINARY_MODE = 0o755


def stage_and_promote_release_binary(
    archive_bytes: bytes,
    *,
    archive_ext: str,
    binary_name: str,
    bin_dir: Path,
    asset_name: str,
) -> None:
    """Extract a release binary to staging and atomically promote it."""

    def write_staged(staged_binary: Path) -> None:
        _extract_binary(
            archive_bytes,
            archive_ext=archive_ext,
            binary_name=binary_name,
            destination=staged_binary,
            asset_name=asset_name,
        )

    _stage_and_promote(binary_name, bin_dir=bin_dir, write_staged=write_staged)


def stage_and_promote_binary_file(
    source: Path,
    *,
    destination: Path,
    prepare_staged: Callable[[Path], None] | None = None,
) -> None:
    """Copy a local binary to staging and atomically promote it."""

    def write_staged(staged_binary: Path) -> None:
        with source.open("rb") as fileobj:
            _write_staged_binary(staged_binary, fileobj)
        if prepare_staged is not None:
            prepare_staged(staged_binary)

    _stage_and_promote(destination.name, bin_dir=destination.parent, write_staged=write_staged)


def _stage_and_promote(
    binary_name: str,
    *,
    bin_dir: Path,
    write_staged: Callable[[Path], None],
) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{binary_name}-staging-", dir=str(bin_dir)))
    try:
        staged_binary = staging_dir / binary_name
        write_staged(staged_binary)
        os.replace(staged_binary, bin_dir / binary_name)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def _extract_binary(
    archive_bytes: bytes,
    *,
    archive_ext: str,
    binary_name: str,
    destination: Path,
    asset_name: str,
) -> None:
    try:
        if archive_ext == "zip":
            with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
                for member_name in archive.namelist():
                    if member_name.endswith(f"/{binary_name}") or member_name == binary_name:
                        with archive.open(member_name) as fileobj:
                            _write_staged_binary(destination, fileobj)
                        return
        else:
            with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r:gz") as archive:
                for member in archive.getmembers():
                    if member.name.endswith(f"/{binary_name}") or member.name == binary_name:
                        extracted_file = archive.extractfile(member)
                        if extracted_file is None:
                            continue
                        with extracted_file:
                            _write_staged_binary(destination, extracted_file)
                        return
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise SourceUnavailableError(f"{asset_name}: extraction failed: {exc}") from exc
    raise SourceUnavailableError(f"{asset_name}: binary {binary_name} not found")


def _write_staged_binary(path: Path, source: IO[bytes]) -> None:
    with path.open("wb") as fileobj:
        shutil.copyfileobj(source, fileobj)
        fileobj.flush()
        os.fsync(fileobj.fileno())
        os.fchmod(fileobj.fileno(), NATIVE_BINARY_MODE)
