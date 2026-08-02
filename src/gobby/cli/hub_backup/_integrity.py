"""Filesystem integrity primitives for hub backup publication and consumption."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import IO, Protocol

import click


class ArtifactLike(Protocol):
    """Manifest artifact fields needed for an integrity check."""

    @property
    def path(self) -> str: ...

    @property
    def sha256(self) -> str: ...

    @property
    def size_bytes(self) -> int: ...


def refuse_symlink_traversal(path: Path, *, label: str) -> None:
    """Reject every existing symlink component without resolving the path."""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise click.ClickException(f"Cannot inspect {label} path {current}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise click.ClickException(f"{label} refuses symlink path component: {current}")


def require_regular_file(path: Path, *, label: str) -> os.stat_result:
    """Require a regular file reached without traversing a symlink."""
    refuse_symlink_traversal(path, label=label)
    try:
        result = path.lstat()
    except FileNotFoundError as exc:
        raise click.ClickException(f"{label} not found: {path}") from exc
    except OSError as exc:
        raise click.ClickException(f"Cannot inspect {label} at {path}: {exc}") from exc
    if not stat.S_ISREG(result.st_mode):
        raise click.ClickException(f"{label} is not a regular file: {path}")
    return result


def open_exclusive_binary(path: Path, *, label: str) -> IO[bytes]:
    """Create an owner-only regular file while refusing an existing leaf."""
    refuse_symlink_traversal(path, label=label)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise click.ClickException(f"Cannot create {label} at {path}: {exc}") from exc
    return os.fdopen(descriptor, "wb")


def read_bytes_no_follow(path: Path, *, label: str) -> bytes:
    """Read a regular file through an O_NOFOLLOW descriptor."""
    with open_regular_binary(path, label=label) as source:
        return source.read()


def open_regular_binary(path: Path, *, label: str) -> IO[bytes]:
    """Open a regular file for streaming without following its leaf."""
    require_regular_file(path, label=label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise click.ClickException(f"Cannot open {label} at {path}: {exc}") from exc
    try:
        result = os.fstat(descriptor)
    except OSError as exc:
        os.close(descriptor)
        raise click.ClickException(f"Cannot inspect open {label} at {path}: {exc}") from exc
    if not stat.S_ISREG(result.st_mode):
        os.close(descriptor)
        raise click.ClickException(f"{label} is not a regular file: {path}")
    try:
        return os.fdopen(descriptor, "rb")
    except OSError as exc:
        os.close(descriptor)
        raise click.ClickException(f"Cannot stream {label} at {path}: {exc}") from exc


def file_digest(path: Path, *, label: str) -> tuple[str, int]:
    """Hash a regular file through a no-follow descriptor."""
    digest = hashlib.sha256()
    size = 0
    with open_regular_binary(path, label=label) as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def artifact_integrity_errors(
    backup_root: Path,
    artifacts: Iterable[ArtifactLike],
) -> list[str]:
    """Return every missing, unsafe, size-mismatched, or hash-mismatched artifact."""
    errors: list[str] = []
    for artifact in artifacts:
        try:
            relpath = _safe_relpath(artifact.path)
            path = backup_root.joinpath(*relpath.parts)
            actual_hash, actual_size = file_digest(path, label=f"backup artifact {artifact.path}")
        except (click.ClickException, ValueError) as exc:
            errors.append(str(exc))
            continue
        if actual_size != artifact.size_bytes:
            errors.append(
                f"artifact size mismatch for {artifact.path}: "
                f"manifest {artifact.size_bytes}, actual {actual_size}"
            )
        if actual_hash != artifact.sha256:
            errors.append(
                f"artifact sha256 mismatch for {artifact.path}: "
                f"manifest {artifact.sha256}, actual {actual_hash}"
            )
    return errors


def verify_artifacts(backup_root: Path, artifacts: Iterable[ArtifactLike]) -> None:
    """Fail unless every artifact still matches its just-recorded digest."""
    errors = artifact_integrity_errors(backup_root, artifacts)
    if errors:
        raise click.ClickException(
            "Backup artifact integrity verification failed: " + "; ".join(errors)
        )


def create_staging_directory(final_root: Path) -> Path:
    """Create a private staging directory beside a required-absent final root."""
    final_root = Path(os.path.abspath(final_root))
    require_absent_output_path(final_root)

    _create_parent_directories(final_root.parent)
    refuse_symlink_traversal(final_root.parent, label="Backup output")
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{final_root.name}.",
            suffix=".tmp",
            dir=final_root.parent,
        )
    )
    staging.chmod(0o700)
    return staging


def require_absent_output_path(final_root: Path) -> None:
    """Refuse a symlinked or pre-existing publication path before preflight."""
    final_root = Path(os.path.abspath(final_root))
    refuse_symlink_traversal(final_root, label="Backup output")
    try:
        final_root.lstat()
    except FileNotFoundError:
        pass
    else:
        raise click.ClickException(f"Backup output path already exists: {final_root}")


def publish_staged_backup(staging: Path, final_root: Path) -> None:
    """Durably rename a complete same-filesystem staging tree into place."""
    final_root = Path(os.path.abspath(final_root))
    refuse_symlink_traversal(staging, label="Backup staging")
    refuse_symlink_traversal(final_root, label="Backup output")
    try:
        final_root.lstat()
    except FileNotFoundError:
        pass
    else:
        raise click.ClickException(f"Backup output path appeared during backup: {final_root}")
    _fsync_tree(staging)
    try:
        os.rename(staging, final_root)
    except OSError as exc:
        raise click.ClickException(
            f"Atomic backup publication failed for {final_root}: {exc}"
        ) from exc
    _fsync_directory(final_root.parent)


def remove_staging_directory(staging: Path) -> None:
    """Remove an unpublished staging directory after an ordinary failure."""
    try:
        mode = staging.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise click.ClickException(f"Backup staging path became unsafe: {staging}")
    shutil.rmtree(staging)


def _safe_relpath(value: str) -> PurePosixPath:
    relpath = PurePosixPath(value)
    if not value or relpath.is_absolute() or ".." in relpath.parts:
        raise ValueError(f"unsafe backup artifact path: {value!r}")
    return relpath


def _create_parent_directories(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while True:
        try:
            current.lstat()
            break
        except FileNotFoundError:
            missing.append(current)
            if current == current.parent:
                break
            current = current.parent
    path.mkdir(parents=True, exist_ok=True)
    for directory in missing:
        directory.chmod(0o700)


def _fsync_tree(root: Path) -> None:
    directories: list[Path] = []
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories.append(current_path)
        for name in [*dirnames, *filenames]:
            refuse_symlink_traversal(current_path / name, label="Backup publication")
        for filename in filenames:
            path = current_path / filename
            require_regular_file(path, label="Backup publication artifact")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
