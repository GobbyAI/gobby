"""Verified archive+wiki container for remote wiki sync-sessions."""

from __future__ import annotations

import os
import stat
import tarfile
import tempfile
from pathlib import Path

MAX_SYNC_MEMBERS = 100_000
MAX_SYNC_UNCOMPRESSED_BYTES = 100 * 1024 * 1024 * 1024


class SyncContainerError(ValueError):
    """Typed refusal while building or verifying a sync-sessions container."""


def build_sync_container(*, archive_dir: Path, wiki_dir: Path) -> Path:
    """Pack no-follow regular files as archives/<name> and wiki/<name>."""
    if not wiki_dir.is_dir():
        raise SyncContainerError("wiki_dir is required")
    if not archive_dir.is_dir():
        raise SyncContainerError("archive_dir is required")
    members = _collect_members(archive_dir, "archives") + _collect_members(wiki_dir, "wiki")
    _preflight(members)
    handle = tempfile.NamedTemporaryFile(prefix="gobby-sync-", suffix=".tar", delete=False)
    dest = Path(handle.name)
    handle.close()
    try:
        with tarfile.open(dest, mode="w") as archive:
            for relative, path, size in members:
                info = tarfile.TarInfo(name=relative)
                info.size = size
                info.type = tarfile.REGTYPE
                with path.open("rb") as payload:
                    copied = 0
                    archive.addfile(info, payload)
                    payload.seek(0, os.SEEK_END)
                    copied = payload.tell()
                if copied != size:
                    raise SyncContainerError(f"copied size mismatch for {relative}")
        return dest
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def _collect_members(root: Path, prefix: str) -> list[tuple[str, Path, int]]:
    members: list[tuple[str, Path, int]] = []
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        if stat.S_ISDIR(st.st_mode):
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise SyncContainerError(f"special file refused: {path}")
        relative = f"{prefix}/{path.relative_to(root).as_posix()}"
        members.append((relative, path, st.st_size))
    return members


def _preflight(members: list[tuple[str, Path, int]]) -> None:
    names = [name for name, _path, _size in members]
    if len(names) != len(set(names)):
        raise SyncContainerError("duplicate member names")
    if len(members) > MAX_SYNC_MEMBERS:
        raise SyncContainerError("member limit exceeded")
    total = sum(size for _name, _path, size in members)
    if total > MAX_SYNC_UNCOMPRESSED_BYTES:
        raise SyncContainerError("uncompressed size limit exceeded")
