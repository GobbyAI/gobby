"""Staged, atomic promotion helpers for managed native binaries."""

from __future__ import annotations

import hashlib
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


# Unpublished managed binaries (gterm, gclient) carry a static crate version, so
# their version stamp can never report staleness. The only honest freshness test
# is the content of the artifact the workspace build just produced, recorded here
# beside the binary so the next install can compare without promoting.
SOURCE_HASH_SUFFIX = "-source-sha256"


def file_sha256(path: Path) -> str:
    """Return the hex sha256 of a file, reading it in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_hash_path(bin_dir: Path, name: str) -> Path:
    """Path of the recorded build-artifact hash for a managed binary."""
    return bin_dir / f".{name}{SOURCE_HASH_SUFFIX}"


def read_source_hash(bin_dir: Path, name: str) -> str | None:
    """Read the recorded build-artifact hash, or None when absent or unreadable."""
    path = source_hash_path(bin_dir, name)
    try:
        if not path.exists():
            return None
        return path.read_text().strip() or None
    except OSError:
        return None


def write_source_hash(bin_dir: Path, name: str, digest: str) -> None:
    """Record the build-artifact hash atomically beside the promoted binary."""
    target = source_hash_path(bin_dir, name)
    fd, tmp_path = tempfile.mkstemp(dir=str(bin_dir), prefix=f".{name}-sha-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(digest + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def clear_source_hash(bin_dir: Path, name: str) -> None:
    """Forget the recorded hash so the next workspace build always promotes."""
    source_hash_path(bin_dir, name).unlink(missing_ok=True)


def workspace_binary_is_current(bin_dir: Path, name: str, built: Path, installed: Path) -> bool:
    """True when the installed binary already came from this exact build artifact."""
    if not installed.exists():
        return False
    recorded = read_source_hash(bin_dir, name)
    return recorded is not None and recorded == file_sha256(built)
