"""
Core path utilities for Gobby package.

This module provides stable path resolution utilities that work in both
development (source) and installed (package) modes without CLI dependencies.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "FilesHomeError",
    "FilesHomeNotOnThisDaemonError",
    "get_package_root",
    "get_install_dir",
    "get_gobby_home",
    "get_files_home",
    "require_files_home",
    "assert_held_files_home_identity",
    "publish_files_home_descendant",
    "ensure_files_home_descendant_dir",
    "open_files_home_descendant",
    "replace_files_home_descendant",
    "unlink_files_home_descendant",
    "fsync_files_home_descendant_dir",
    "files_home_root_fd",
    "get_global_workflows_dir",
    "get_global_rules_dir",
    "get_global_pipelines_dir",
    "get_global_agents_dir",
    "get_global_variables_dir",
    "get_project_workflows_dir",
    "get_project_rules_dir",
    "get_project_pipelines_dir",
    "get_project_agents_dir",
    "get_project_variables_dir",
]


class FilesHomeError(Exception):
    """Raised when this process cannot use the configured files_home."""


class FilesHomeNotOnThisDaemonError(FilesHomeError):
    """Raised when files_home is requested on a remote-mode daemon."""


def get_package_root() -> Path:
    """Get the root directory of the gobby package.

    Returns:
        Path to src/gobby/ (the package root directory)
    """
    import gobby

    return Path(gobby.__file__).parent


def get_install_dir() -> Path:
    """Get the gobby install directory.

    Checks for source directory (development mode) first,
    falls back to package directory. This handles both:
    - Development: src/gobby/install/
    - Installed package: <site-packages>/gobby/install/

    Returns:
        Path to the install directory
    """
    import gobby

    package_install_dir = Path(gobby.__file__).parent / "install"

    # Try to find source directory (project root) for development mode
    current = Path(gobby.__file__).resolve()
    source_install_dir = None

    for parent in current.parents:
        potential_source = parent / "src" / "gobby" / "install"
        if potential_source.exists():
            source_install_dir = potential_source
            break

    if source_install_dir and source_install_dir.exists():
        return source_install_dir
    return package_install_dir


# ---------------------------------------------------------------------------
# Global user template directories (~/.gobby/workflows/<type>/)
# ---------------------------------------------------------------------------


def get_gobby_home() -> Path:
    """Get the gobby home directory (~/.gobby or $GOBBY_HOME)."""
    configured_home = os.environ.get("GOBBY_HOME")
    if configured_home is None or not configured_home.strip():
        return Path.home() / ".gobby"
    return Path(configured_home).expanduser()


@dataclass
class _HeldFilesHome:
    path: Path
    fd: int
    identity: tuple[int, int]
    ancestors: tuple[tuple[int, int], ...]


_HELD_FILES_HOME: _HeldFilesHome | None = None


def get_files_home() -> Path | None:
    """Local-mode files_home, or None on a remote-mode or owner-less daemon."""
    from gobby.config.bootstrap import load_bootstrap

    config = load_bootstrap()
    if config.datastore_mode == "remote" or not config.files_home:
        return None
    return Path(config.files_home)


def require_files_home() -> Path:
    """Return files_home after opening and holding the directory fd."""
    from gobby.config.bootstrap import load_bootstrap

    config = load_bootstrap()
    if config.datastore_mode == "remote":
        raise FilesHomeNotOnThisDaemonError("files_home is not on this remote-mode daemon")
    if not config.files_home:
        raise FilesHomeError("files_home is not configured on this daemon")
    path = Path(config.files_home)
    if path.is_symlink() or not path.is_dir():
        raise FilesHomeError(f"files_home is missing or not a directory: {path}")
    return _hold_files_home(path)


def publish_files_home_descendant(relative: str | Path, content: bytes) -> None:
    """Publish a descendant file through the held files_home fd (no-follow)."""
    held = _require_held_files_home()
    parts = _relative_parts(relative)
    directory_fd, opened = _open_descendant_dir(held.fd, parts[:-1])
    try:
        _durable_replace_at(directory_fd, parts[-1], content)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def ensure_files_home_descendant_dir(relative: str | Path) -> None:
    """Create descendant directories through the held files_home fd."""
    held = _require_held_files_home()
    parts = _relative_parts(relative)
    _directory_fd, opened = _open_descendant_dir(held.fd, parts)
    for fd in reversed(opened):
        os.close(fd)


def open_files_home_descendant(
    relative: str | Path,
    flags: int,
    *,
    mode: int = 0o600,
    create_parents: bool = False,
) -> int:
    """Open a descendant file through the held files_home fd (no-follow)."""
    held = _require_held_files_home()
    parts = _relative_parts(relative)
    opener = _open_descendant_dir if create_parents else _open_existing_descendant_dir
    directory_fd, opened = opener(held.fd, parts[:-1])
    open_flags = flags
    if hasattr(os, "O_CLOEXEC"):
        open_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    try:
        return os.open(parts[-1], open_flags, mode, dir_fd=directory_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise FilesHomeError(f"cannot open files_home descendant: {exc}") from exc
    finally:
        for fd in reversed(opened):
            os.close(fd)


def replace_files_home_descendant(source: str | Path, destination: str | Path) -> None:
    """Atomically replace one descendant with another through the held fd."""
    held = _require_held_files_home()
    source_parts = _relative_parts(source)
    destination_parts = _relative_parts(destination)
    source_dir_fd, source_opened = _open_existing_descendant_dir(held.fd, source_parts[:-1])
    try:
        dest_dir_fd, dest_opened = _open_existing_descendant_dir(held.fd, destination_parts[:-1])
        try:
            os.replace(
                source_parts[-1],
                destination_parts[-1],
                src_dir_fd=source_dir_fd,
                dst_dir_fd=dest_dir_fd,
            )
        except OSError as exc:
            raise FilesHomeError(f"cannot replace files_home descendant: {exc}") from exc
        finally:
            for fd in reversed(dest_opened):
                os.close(fd)
    finally:
        for fd in reversed(source_opened):
            os.close(fd)


def unlink_files_home_descendant(relative: str | Path) -> None:
    """Unlink a descendant through the held files_home fd (no-follow)."""
    held = _require_held_files_home()
    parts = _relative_parts(relative)
    directory_fd, opened = _open_existing_descendant_dir(held.fd, parts[:-1])
    try:
        os.unlink(parts[-1], dir_fd=directory_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise FilesHomeError(f"cannot unlink files_home descendant: {exc}") from exc
    finally:
        for fd in reversed(opened):
            os.close(fd)


def fsync_files_home_descendant_dir(relative: str | Path) -> None:
    """Fsync a descendant directory through the held files_home fd."""
    held = _require_held_files_home()
    parts = _relative_parts(relative)
    directory_fd, opened = _open_existing_descendant_dir(held.fd, parts)
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        raise FilesHomeError(f"cannot fsync files_home descendant: {exc}") from exc
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _hold_files_home(path: Path) -> Path:
    global _HELD_FILES_HOME
    _release_held_files_home()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise FilesHomeError(f"cannot open files_home: {exc}") from exc
    try:
        stat_result = os.fstat(fd)
        held = _HeldFilesHome(
            path=path,
            fd=fd,
            identity=(stat_result.st_dev, stat_result.st_ino),
            ancestors=_ancestor_identities(path),
        )
    except OSError as exc:
        os.close(fd)
        raise FilesHomeError(f"cannot stat files_home: {exc}") from exc
    _HELD_FILES_HOME = held
    return path.resolve()


def _require_held_files_home() -> _HeldFilesHome:
    if _HELD_FILES_HOME is None:
        raise FilesHomeError("require_files_home has not been called")
    _assert_files_home_identity(_HELD_FILES_HOME)
    return _HELD_FILES_HOME


def assert_held_files_home_identity() -> None:
    """Re-check the held files_home root and ancestor identities."""
    _assert_files_home_identity(_require_held_files_home())


def files_home_root_fd() -> int:
    """Borrowed held files_home directory fd. Callers must not close it."""
    return _require_held_files_home().fd


def _assert_files_home_identity(held: _HeldFilesHome) -> None:
    try:
        current = _ancestor_identities(held.path)
    except OSError as exc:
        raise FilesHomeError(f"files_home identity changed: {exc}") from exc
    if current != held.ancestors:
        raise FilesHomeError("files_home root or ancestor identity changed")
    try:
        stat_result = os.lstat(held.path)
    except OSError as exc:
        raise FilesHomeError(f"files_home identity changed: {exc}") from exc
    if (stat_result.st_dev, stat_result.st_ino) != held.identity:
        raise FilesHomeError("files_home root or ancestor identity changed")


def _ancestor_identities(path: Path) -> tuple[tuple[int, int], ...]:
    identities: list[tuple[int, int]] = []
    current = path
    while True:
        stat_result = os.lstat(current)
        identities.append((stat_result.st_dev, stat_result.st_ino))
        parent = current.parent
        if parent == current:
            break
        current = parent
    return tuple(identities)


def _relative_parts(relative: str | Path) -> tuple[str, ...]:
    rel = Path(relative)
    if (
        rel.is_absolute()
        or not rel.parts
        or ".." in rel.parts
        or any(part == "." for part in rel.parts)
    ):
        raise FilesHomeError("descendant path must be a relative path")
    return rel.parts


def _open_existing_descendant_dir(root_fd: int, parts: tuple[str, ...]) -> tuple[int, list[int]]:
    current = root_fd
    opened: list[int] = []
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for part in parts:
        try:
            next_fd = os.open(part, flags, dir_fd=current)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise FilesHomeError(f"cannot open files_home descendant: {exc}") from exc
        opened.append(next_fd)
        current = next_fd
    return current, opened


def _open_descendant_dir(root_fd: int, parts: tuple[str, ...]) -> tuple[int, list[int]]:
    current = root_fd
    opened: list[int] = []
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for part in parts:
        try:
            next_fd = os.open(part, flags, dir_fd=current)
        except FileNotFoundError:
            os.mkdir(part, 0o700, dir_fd=current)
            next_fd = os.open(part, flags, dir_fd=current)
        except OSError as exc:
            raise FilesHomeError(f"cannot open files_home descendant: {exc}") from exc
        opened.append(next_fd)
        current = next_fd
    return current, opened


def _durable_replace_at(dir_fd: int, name: str, content: bytes) -> None:
    tmp_name = f".{name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(tmp_name, flags, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        raise FilesHomeError(f"cannot publish files_home descendant: {exc}") from exc
    try:
        remaining = memoryview(content)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise FilesHomeError("cannot publish files_home descendant")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except OSError as exc:
        os.unlink(tmp_name, dir_fd=dir_fd)
        raise FilesHomeError(f"cannot publish files_home descendant: {exc}") from exc


def _release_held_files_home() -> None:
    global _HELD_FILES_HOME
    held = _HELD_FILES_HOME
    _HELD_FILES_HOME = None
    if held is None:
        return
    os.close(held.fd)


def get_global_workflows_dir() -> Path:
    """Get the global user workflows root: ~/.gobby/workflows/."""
    return get_gobby_home() / "workflows"


def get_global_rules_dir() -> Path:
    """Get the global user rules directory: ~/.gobby/workflows/rules/."""
    return get_global_workflows_dir() / "rules"


def get_global_pipelines_dir() -> Path:
    """Get the global user pipelines directory: ~/.gobby/workflows/pipelines/."""
    return get_global_workflows_dir() / "pipelines"


def get_global_agents_dir() -> Path:
    """Get the global user agents directory: ~/.gobby/workflows/agents/."""
    return get_global_workflows_dir() / "agents"


def get_global_variables_dir() -> Path:
    """Get the global user variables directory: ~/.gobby/workflows/variables/."""
    return get_global_workflows_dir() / "variables"


def get_global_mcp_templates_dir() -> Path:
    """Get the global user MCP templates directory: ~/.gobby/mcp/templates/."""
    return get_gobby_home() / "mcp" / "templates"


def get_global_mcp_servers_dir() -> Path:
    """Get the global user MCP server instance directory: ~/.gobby/mcp/servers/."""
    return get_gobby_home() / "mcp" / "servers"


# ---------------------------------------------------------------------------
# Project-scoped template directories (.gobby/workflows/<type>/)
# ---------------------------------------------------------------------------


def get_project_workflows_dir(project_path: Path) -> Path:
    """Get project workflows root: <project>/.gobby/workflows/."""
    return project_path / ".gobby" / "workflows"


def get_project_rules_dir(project_path: Path) -> Path:
    """Get project rules directory: <project>/.gobby/workflows/rules/."""
    return get_project_workflows_dir(project_path) / "rules"


def get_project_pipelines_dir(project_path: Path) -> Path:
    """Get project pipelines directory: <project>/.gobby/workflows/pipelines/."""
    return get_project_workflows_dir(project_path) / "pipelines"


def get_project_agents_dir(project_path: Path) -> Path:
    """Get project agents directory: <project>/.gobby/workflows/agents/."""
    return get_project_workflows_dir(project_path) / "agents"


def get_project_variables_dir(project_path: Path) -> Path:
    """Get project variables directory: <project>/.gobby/workflows/variables/."""
    return get_project_workflows_dir(project_path) / "variables"


def get_project_mcp_templates_dir(project_path: Path) -> Path:
    """Get project MCP templates directory: <project>/.gobby/mcp/templates/."""
    return project_path / ".gobby" / "mcp" / "templates"


def get_project_mcp_servers_dir(project_path: Path) -> Path:
    """Get project MCP server instance directory: <project>/.gobby/mcp/servers/."""
    return project_path / ".gobby" / "mcp" / "servers"
