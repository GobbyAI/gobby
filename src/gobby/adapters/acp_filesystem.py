"""ACP v1 client filesystem helpers."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


class ACPFileSystemError(Exception):
    """Error that should be returned to the ACP agent as a JSON-RPC error."""

    def __init__(self, message: str, *, code: int = -32602) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ACPResolvedPath:
    path: Path
    root: Path


def roots_for_client(client: Any) -> tuple[Path, ...]:
    """Resolve usable session roots for an ACP client."""
    roots: tuple[str, ...] = ()
    session_state = getattr(client, "_session_state", None)
    if session_state is not None:
        raw_roots = getattr(session_state, "root_uris", ())
        if isinstance(raw_roots, tuple):
            roots = tuple(root for root in raw_roots if isinstance(root, str) and root)

    if not roots:
        cwd = getattr(client, "_cwd", None)
        if isinstance(cwd, str) and cwd.strip():
            roots = (cwd,)

    return normalize_roots(roots)


def normalize_roots(values: Iterable[str]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for value in values:
        root = _root_path_from_value(value)
        if root is None:
            continue
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def resolve_file_path(path_value: Any, roots: Iterable[Path]) -> ACPResolvedPath:
    if not isinstance(path_value, str) or not path_value.strip():
        raise ACPFileSystemError("path must be a non-empty string")

    target = Path(path_value)
    if not target.is_absolute():
        raise ACPFileSystemError("path must be absolute")

    target = target.resolve(strict=False)
    root_tuple = tuple(roots)
    if not root_tuple:
        raise ACPFileSystemError("No ACP session roots are available", code=-32000)

    for root in root_tuple:
        if target == root or target.is_relative_to(root):
            resolved = ACPResolvedPath(path=target, root=root)
            _reject_git_path(resolved)
            return resolved

    raise ACPFileSystemError("path is outside the ACP session root")


def read_text_file(path_value: Any, roots: Iterable[Path], *, line: Any, limit: Any) -> str:
    resolved = resolve_file_path(path_value, roots)
    start_line = _optional_int(line, "line", minimum=1)
    line_limit = _optional_int(limit, "limit", minimum=0)

    try:
        content = resolved.path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ACPFileSystemError("file does not exist", code=-32000) from exc
    except UnicodeDecodeError as exc:
        raise ACPFileSystemError("file is not valid UTF-8 text", code=-32000) from exc
    except OSError as exc:
        raise ACPFileSystemError(f"failed to read file: {exc}", code=-32000) from exc

    if start_line is None and line_limit is None:
        return content

    lines = content.splitlines(keepends=True)
    start_index = (start_line - 1) if start_line is not None else 0
    selected = lines[start_index:]
    if line_limit is not None:
        selected = selected[:line_limit]
    return "".join(selected)


def write_text_file(path_value: Any, roots: Iterable[Path], *, content: Any) -> int:
    resolved = resolve_file_path(path_value, roots)
    if not isinstance(content, str):
        raise ACPFileSystemError("content must be a string")
    if not resolved.path.parent.is_dir():
        raise ACPFileSystemError("parent directory does not exist", code=-32000)

    encoded = content.encode("utf-8")
    try:
        existing_mode = stat.S_IMODE(resolved.path.stat().st_mode)
    except FileNotFoundError:
        existing_mode = None
    except OSError as exc:
        raise ACPFileSystemError("failed to inspect target file", code=-32000) from exc

    try:
        fd, temp_path = tempfile.mkstemp(
            dir=os.fspath(resolved.path.parent),
            prefix=f".{resolved.path.name}.",
            suffix=".tmp",
        )
    except OSError as exc:
        raise ACPFileSystemError("failed to create temporary file", code=-32000) from exc

    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if existing_mode is not None:
            os.chmod(temp_path, existing_mode)
        os.replace(temp_path, resolved.path)
    except OSError as exc:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise ACPFileSystemError("failed to write file", code=-32000) from exc
    return len(encoded)


def _root_path_from_value(value: str) -> Path | None:
    parsed = urlparse(value)
    if parsed.scheme:
        if parsed.scheme != "file":
            return None
        if parsed.netloc not in {"", "localhost"}:
            return None
        path_text = unquote(parsed.path)
    else:
        path_text = value
    if not path_text.strip():
        return None
    return Path(path_text).resolve(strict=False)


def _reject_git_path(resolved: ACPResolvedPath) -> None:
    relative_parts = resolved.path.relative_to(resolved.root).parts
    if ".git" in relative_parts:
        raise ACPFileSystemError("access to .git paths is not allowed")


def _optional_int(value: Any, name: str, *, minimum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ACPFileSystemError(f"{name} must be an integer")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        raise ACPFileSystemError(f"{name} must be an integer")
    if number < minimum:
        raise ACPFileSystemError(f"{name} must be >= {minimum}")
    return number
