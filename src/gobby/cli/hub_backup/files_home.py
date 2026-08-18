"""Archive and restore the hub-owned files_home bind for pack and hub-backup."""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from gobby.cli.hub_backup._integrity import file_digest
from gobby.cli.hub_backup._manifest import ArtifactRecord, VerificationState
from gobby.files_migrate import special_file_reason
from gobby.paths import (
    ensure_files_home_descendant_dir,
    get_gobby_home,
    require_files_home,
)
from gobby.runner_pid_file import PidFileClaim, claim_pid_file
from gobby.utils.durable_file import durable_replace_files_home

MAX_ARCHIVE_MEMBERS = 100_000
MAX_ARCHIVE_BYTES = 100 * 1024**3
COPY_CHUNK = 1024 * 1024
PACK_FILES_PREFIX = "gobby/files"
FILES_ARCHIVE_RELPATH = "files/files_home.tar"
FILES_STORE_KEY = "files"
FILES_ARCHIVE_METHOD = "files-home-prewalk+sha256"


class FilesHomeArchiveError(Exception):
    """Typed refusal for files_home archive or restore."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WalkEntry:
    rel: str
    is_dir: bool
    size: int
    dev: int = 0
    ino: int = 0
    nlink: int = 1
    mode: int = 0


@dataclass
class FilesHomeArchiveHooks:
    after_prewalk: Callable[[list[WalkEntry]], None] | None = None
    fail_temp_write: bool = False
    fail_fsync: bool = False
    fail_replace: bool = False
    fail_mid_member: bool = False
    declared_size_override: int | None = None
    free_bytes: int | None = None
    swap_after_confirm: Callable[[], None] | None = None
    on_claimed: Callable[[PidFileClaim], None] | None = None
    force_member_count: int | None = None


_active_hooks: FilesHomeArchiveHooks | None = None


def active_hooks() -> FilesHomeArchiveHooks:
    return _active_hooks or FilesHomeArchiveHooks()


def destination_free_bytes(dest: Path) -> int:
    hooks = active_hooks()
    if hooks.free_bytes is not None:
        return hooks.free_bytes
    probe = dest if dest.exists() else dest.parent
    return shutil.disk_usage(probe).free


def require_destination_files_home() -> Path:
    bootstrap = get_gobby_home() / "bootstrap.yaml"
    if not bootstrap.is_file():
        raise FilesHomeArchiveError("bootstrap", "destination bootstrap.yaml is required")
    return require_files_home()


@contextmanager
def maintenance_claim(gobby_home: Path | None = None) -> Iterator[PidFileClaim]:
    claim = claim_pid_file((gobby_home or get_gobby_home()) / "gobby.pid", role="maintenance")
    if claim is None:
        raise FilesHomeArchiveError(
            "claim",
            "cannot run while the hub daemon or another maintenance campaign is running",
        )
    try:
        hooks = active_hooks()
        if hooks.on_claimed is not None:
            hooks.on_claimed(claim)
        yield claim
    finally:
        claim.release()


def check_output_outside_sources(output: Path, *sources: Path) -> None:
    resolved = output.expanduser().resolve()
    candidates = (resolved, resolved.parent)
    for source in sources:
        root = source.expanduser().resolve()
        for candidate in candidates:
            if candidate == root or root in candidate.parents:
                raise FilesHomeArchiveError(
                    "output_inside_source",
                    f"output path {candidate} is inside archived source {root}",
                )


def prewalk_files_home(root: Path) -> list[WalkEntry]:
    entries: list[WalkEntry] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        directory = Path(dirpath)
        dirnames.sort()
        filenames.sort()
        rel_dir = directory.relative_to(root).as_posix()
        if rel_dir != ".":
            entries.append(_entry_from_lstat(directory, rel_dir, is_dir=True))
        for name in filenames:
            path = directory / name
            rel = path.relative_to(root).as_posix()
            entries.append(_entry_from_lstat(path, rel, is_dir=False))
    return entries


def _entry_from_lstat(path: Path, rel: str, *, is_dir: bool) -> WalkEntry:
    stat_result = os.lstat(path)
    reason = special_file_reason(stat_result)
    if reason is not None:
        raise FilesHomeArchiveError("special", f"files_home contains {reason}: {rel}")
    return WalkEntry(
        rel=rel,
        is_dir=is_dir,
        size=0 if is_dir else stat_result.st_size,
        dev=stat_result.st_dev,
        ino=stat_result.st_ino,
        nlink=stat_result.st_nlink,
        mode=stat_result.st_mode,
    )


def _item_name_and_dir(item: WalkEntry | tarfile.TarInfo) -> tuple[str, bool, int]:
    if isinstance(item, WalkEntry):
        return item.rel.replace("\\", "/").strip("/"), item.is_dir, item.size
    return item.name.replace("\\", "/").strip("/"), item.isdir(), int(item.size)


def preflight_archive_graph(
    items: Sequence[WalkEntry | tarfile.TarInfo],
    *,
    extra_members: int = 0,
) -> None:
    names: dict[str, bool] = {}
    total = extra_members
    size = 0
    for item in items:
        name, is_dir, member_size = _item_name_and_dir(item)
        if not name:
            continue
        if name in names:
            raise FilesHomeArchiveError("duplicate", f"duplicate archive member {name}")
        names[name] = is_dir
        total += 1
        if not is_dir:
            if member_size < 0 or size > MAX_ARCHIVE_BYTES - member_size:
                raise FilesHomeArchiveError(
                    "limit",
                    f"archive exceeds {MAX_ARCHIVE_BYTES} uncompressed bytes",
                )
            size += member_size
    if total > MAX_ARCHIVE_MEMBERS:
        raise FilesHomeArchiveError(
            "limit",
            f"archive has more than {MAX_ARCHIVE_MEMBERS:,} members",
        )
    for name in names:
        ancestor = ""
        for part in name.split("/")[:-1]:
            ancestor = f"{ancestor}/{part}" if ancestor else part
            if ancestor in names and not names[ancestor]:
                raise FilesHomeArchiveError("prefix", f"file/directory prefix conflict: {ancestor}")


def _strip_prefix(name: str, prefix: str) -> str:
    cleaned = name.replace("\\", "/").strip("/")
    if not prefix:
        return cleaned
    prefix = prefix.strip("/")
    if cleaned == prefix:
        return ""
    head = f"{prefix}/"
    if cleaned.startswith(head):
        return cleaned[len(head) :]
    return cleaned


def files_members_would_overwrite(
    members: Sequence[tarfile.TarInfo],
    dest: Path,
    prefix: str = "",
) -> bool:
    return any(
        (rel := _strip_prefix(member.name, prefix)) and (dest / rel).exists() for member in members
    )


def write_restricted_archive(
    output_path: Path,
    items: list[tuple[str, Path]],
    *,
    files_home: Path | None = None,
    source_roots: Sequence[Path] = (),
    gzip: bool = True,
) -> None:
    hooks = active_hooks()
    roots = [Path(root) for root in source_roots]
    if files_home is not None:
        roots.append(files_home)
    check_output_outside_sources(output_path, *roots)
    entries: list[WalkEntry] | None = None
    if files_home is not None:
        entries = prewalk_files_home(files_home)
        extra = 0
        if hooks.force_member_count is not None:
            extra = max(0, hooks.force_member_count - len(entries))
        preflight_archive_graph(entries, extra_members=extra)
    temp_dir = _temp_dir_outside(output_path, roots)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=str(temp_dir)
    )
    temp_path = Path(temp_name)
    try:
        if hooks.fail_temp_write:
            raise FilesHomeArchiveError("temp_write", "injected temp write failure")
        with os.fdopen(fd, "wb") as raw:
            fd = -1
            tar = (
                tarfile.open(fileobj=raw, mode="w:gz")
                if gzip
                else tarfile.open(fileobj=raw, mode="w")
            )
            with tar:
                for archive_name, path in items:
                    if files_home is not None and path.resolve() == files_home.resolve():
                        if entries is not None:
                            _emit_entries(tar, entries, archive_name, hooks)
                        continue
                    tar.add(str(path), arcname=archive_name)
            if hooks.fail_fsync:
                raise FilesHomeArchiveError("fsync", "injected fsync failure")
            raw.flush()
            os.fsync(raw.fileno())
        temp_path.chmod(0o600)
        if hooks.fail_replace:
            raise FilesHomeArchiveError("replace", "injected replace failure")
        os.replace(temp_path, output_path)
        output_path.chmod(0o600)
        dir_fd = os.open(output_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)
        raise


def _temp_dir_outside(output_path: Path, roots: Sequence[Path]) -> Path:
    parent = output_path.expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        check_output_outside_sources(parent / ".probe", *roots)
    except FilesHomeArchiveError:
        return Path(tempfile.gettempdir())
    return parent


def _emit_entries(
    tar: tarfile.TarFile,
    entries: list[WalkEntry],
    prefix: str,
    hooks: FilesHomeArchiveHooks,
) -> None:
    from gobby.paths import open_files_home_descendant

    require_files_home()
    if hooks.after_prewalk is not None:
        hooks.after_prewalk(entries)
    emitted = 0
    for entry in entries:
        if hooks.fail_mid_member and emitted:
            raise FilesHomeArchiveError("mid_member", "injected mid-member failure")
        arcname = f"{prefix.strip('/')}/{entry.rel}" if prefix else entry.rel
        info = tarfile.TarInfo(arcname)
        if entry.is_dir:
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
            emitted += 1
            continue
        info.size = entry.size
        info.type = tarfile.REGTYPE
        descriptor = open_files_home_descendant(entry.rel, os.O_RDONLY)
        handle: IO[bytes] | None = None
        try:
            stat_result = os.fstat(descriptor)
            changed = (
                stat_result.st_dev,
                stat_result.st_ino,
                stat_result.st_size,
                stat_result.st_nlink,
            ) != (entry.dev, entry.ino, entry.size, entry.nlink) or special_file_reason(
                stat_result
            ) is not None
            if changed:
                raise FilesHomeArchiveError("swap", "files_home entry changed after prewalk")
            handle = os.fdopen(descriptor, "rb")
            descriptor = -1
            tar.addfile(info, handle)
        finally:
            if handle is not None:
                handle.close()
            elif descriptor >= 0:
                os.close(descriptor)
        emitted += 1


def archive_files_home_store(
    backup_root: Path,
    files_home: Path | None = None,
) -> tuple[list[ArtifactRecord], dict[str, object]]:
    root = files_home or require_files_home()
    dest = backup_root / FILES_ARCHIVE_RELPATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_restricted_archive(
        dest,
        [("", root)],
        files_home=root,
        source_roots=(root,),
        gzip=False,
    )
    digest, size = file_digest(dest, label="files_home archive")
    return (
        [
            ArtifactRecord(
                name="files-home",
                path=FILES_ARCHIVE_RELPATH,
                sha256=digest,
                size_bytes=size,
            )
        ],
        {"members": len(prewalk_files_home(root)), "sha256": digest},
    )


def verify_files_home_archive(archive: Path) -> tuple[VerificationState, dict[str, object]]:
    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
        preflight_archive_graph(members)
        for member in members:
            if not member.isfile() and not member.isdir():
                raise FilesHomeArchiveError(
                    "invalid", f"invalid files archive member {member.name}"
                )
    return (
        VerificationState(
            verified=True,
            method=FILES_ARCHIVE_METHOD,
            timestamp=datetime.now(UTC).isoformat(),
        ),
        {"members": len(members)},
    )


def restore_files_home_from_archive(
    archive: Path | tarfile.TarFile,
    dest: Path,
    *,
    prefix: str = "",
    expected_sha256: str | None = None,
    hooks: FilesHomeArchiveHooks | None = None,
    hold_claim: bool = True,
) -> None:
    hooks = hooks or active_hooks()
    if hold_claim:
        with maintenance_claim() as claim:
            if hooks.on_claimed is not None:
                hooks.on_claimed(claim)
            _restore_body(archive, dest, prefix, expected_sha256, hooks)
        return
    _restore_body(archive, dest, prefix, expected_sha256, hooks)


def _restore_body(
    archive: Path | tarfile.TarFile,
    dest: Path,
    prefix: str,
    expected_sha256: str | None,
    hooks: FilesHomeArchiveHooks,
) -> None:
    path = archive if isinstance(archive, Path) else None
    if path is not None and expected_sha256 is not None:
        digest, _size = file_digest(path, label="files archive")
        if digest != expected_sha256:
            raise FilesHomeArchiveError("hash", "files archive hash mismatch")
    if hooks.swap_after_confirm is not None:
        hooks.swap_after_confirm()
        if path is not None and expected_sha256 is not None:
            digest, _size = file_digest(path, label="files archive")
            if digest != expected_sha256:
                raise FilesHomeArchiveError("swap", "files archive swapped after confirmation")
    closer: tarfile.TarFile | None = None
    tar = archive if isinstance(archive, tarfile.TarFile) else tarfile.open(archive, "r:*")
    if not isinstance(archive, tarfile.TarFile):
        closer = tar
    try:
        members = [
            member
            for member in tar.getmembers()
            if not prefix
            or member.name.rstrip("/") == prefix.strip("/")
            or member.name.startswith(f"{prefix.strip('/')}/")
        ]
        preflight_archive_graph(members)
        for member in members:
            if not member.isfile() and not member.isdir():
                raise FilesHomeArchiveError(
                    "invalid",
                    f"Unsafe archive member {member.name!r}: only regular files and directories are supported",
                )
        needed = sum(member.size for member in members if member.isfile())
        if destination_free_bytes(dest) < needed:
            raise FilesHomeArchiveError(
                "space", "insufficient destination space for files_home restore"
            )
        require_files_home()
        published = 0
        for member in members:
            rel = _strip_prefix(member.name, prefix)
            if not rel:
                continue
            if member.isdir():
                ensure_files_home_descendant_dir(rel)
                continue
            source = tar.extractfile(member)
            if source is None:
                raise FilesHomeArchiveError("invalid", f"missing member {member.name}")
            declared = (
                hooks.declared_size_override
                if hooks.declared_size_override is not None
                else member.size
            )
            _publish_member(source, rel, declared, hooks)
            published += 1
    finally:
        if closer is not None:
            closer.close()


def _publish_member(
    source: IO[bytes],
    rel: str,
    declared: int,
    hooks: FilesHomeArchiveHooks,
) -> None:
    if hooks.fail_temp_write:
        raise FilesHomeArchiveError("temp_write", "injected temp write failure")
    fd, temp_name = tempfile.mkstemp(prefix=".files-restore-", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        copied = 0
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            while chunk := source.read(COPY_CHUNK):
                handle.write(chunk)
                copied += len(chunk)
            if hooks.fail_fsync:
                raise FilesHomeArchiveError("fsync", "injected fsync failure")
            handle.flush()
            os.fsync(handle.fileno())
        if copied != declared:
            raise FilesHomeArchiveError(
                "size",
                "copied bytes disagree with declared member size",
            )
        if hooks.fail_replace:
            raise FilesHomeArchiveError("replace", "injected replace failure")
        if hooks.fail_mid_member:
            raise FilesHomeArchiveError("mid_member", "injected mid-member failure")
        parent = str(Path(rel).parent)
        temp_locator = f"{rel}.restore-tmp" if parent != "." else f".{rel}.restore-tmp"
        if parent != ".":
            temp_locator = f"{parent}/.{Path(rel).name}.restore-tmp"
        durable_replace_files_home(temp_path, rel, temp_locator)
    finally:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)


def merge_bootstrap_preserving_files_home(
    dest_bootstrap: Path,
    archived: bytes,
    dest_files_home: Path,
) -> None:
    import yaml

    from gobby.config.bootstrap_io import write_bootstrap_yaml

    loaded = yaml.safe_load(archived.decode("utf-8"))
    data = loaded if isinstance(loaded, dict) else {}
    data["files_home"] = str(dest_files_home)
    data.setdefault("datastore_mode", "local")
    write_bootstrap_yaml(dest_bootstrap, data)


def restore_hub_files(backup_root: Path, expected_sha256: str | None = None) -> None:
    dest = require_destination_files_home()
    archive = backup_root / FILES_ARCHIVE_RELPATH
    if not archive.is_file():
        raise FilesHomeArchiveError("missing", "hub backup has no files_home archive")
    restore_files_home_from_archive(
        archive,
        dest,
        expected_sha256=expected_sha256,
        hold_claim=False,
    )
