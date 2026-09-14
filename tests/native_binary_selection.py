"""Explicit native-binary selection for terminal test harnesses."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

NATIVE_BIN_DIR_ENV = "GOBBY_NATIVE_BIN_DIR"


class NativeBinarySelectionError(RuntimeError):
    """A configured terminal-test binary cannot be used safely."""


@dataclass(frozen=True, slots=True)
class SelectedNativeBinary:
    """A validated executable and the provenance needed in test evidence."""

    name: str
    source: str
    path: Path
    sha256: str
    device: int
    inode: int

    def header(self) -> str:
        """Return the reproducible run-header representation."""
        return (
            f"native-binary name={self.name} source={self.source} path={self.path} "
            f"sha256={self.sha256} device={self.device} inode={self.inode}"
        )


def _selection_root() -> tuple[Path, str, bool]:
    if NATIVE_BIN_DIR_ENV in os.environ:
        configured = os.environ[NATIVE_BIN_DIR_ENV]
        if configured == "":
            raise NativeBinarySelectionError(f"{NATIVE_BIN_DIR_ENV} is explicitly empty")
        root = Path(configured).expanduser().resolve()
        if not root.is_dir():
            raise NativeBinarySelectionError(
                f"{NATIVE_BIN_DIR_ENV} must name an existing directory: {root}"
            )
        return root, NATIVE_BIN_DIR_ENV, True
    return (Path.home() / ".gobby" / "bin").resolve(), "installed-default", False


def _inspect_binary(path: Path, *, name: str, source: str) -> SelectedNativeBinary:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise NativeBinarySelectionError(f"native binary is unavailable: {path}") from exc
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISREG(path_stat.st_mode)
        or not os.access(path, os.X_OK)
    ):
        raise NativeBinarySelectionError(f"native binary must be a regular executable: {path}")
    try:
        absolute = path.resolve(strict=True)
        with absolute.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            opened_stat = os.fstat(stream.fileno())
    except OSError as exc:
        raise NativeBinarySelectionError(f"native binary could not be read: {path}") from exc
    return SelectedNativeBinary(
        name=name,
        source=source,
        path=absolute,
        sha256=digest,
        device=opened_stat.st_dev,
        inode=opened_stat.st_ino,
    )


def select_native_binary(name: str, *, required: bool) -> SelectedNativeBinary | None:
    """Select one executable from the explicit override or installed directory.

    ``required`` controls only a missing default installation. An explicit override
    is always authoritative and invalid configuration always raises.
    """
    root, source, explicit = _selection_root()
    candidate = root / name
    if not candidate.exists() and not candidate.is_symlink():
        if not explicit and not required:
            return None
        display = candidate if explicit else Path("~/.gobby/bin") / name
        raise NativeBinarySelectionError(f"native binary is unavailable: {display}")
    return _inspect_binary(candidate, name=name, source=source)


def select_native_binaries(
    names: Iterable[str],
    *,
    required: bool,
) -> tuple[SelectedNativeBinary, ...]:
    """Select a stable ordered set of terminal-harness executables."""
    selected: list[SelectedNativeBinary] = []
    for name in dict.fromkeys(names):
        binary = select_native_binary(name, required=required)
        if binary is not None:
            selected.append(binary)
    return tuple(selected)


def install_native_binary(source: Path, destination: Path) -> SelectedNativeBinary:
    """Install a harness-owned executable atomically through a new inode."""
    inspected_source = _inspect_binary(source, name=source.name, source="harness-source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = destination.parent.resolve(strict=True) / destination.name
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}-",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(inspected_source.path, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return _inspect_binary(destination, name=destination.name, source="harness-managed")
