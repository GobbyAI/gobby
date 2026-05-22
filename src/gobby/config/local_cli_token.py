"""Machine-local token used by trusted local CLI helpers."""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path

from .bootstrap_io import default_gobby_home

LOCAL_CLI_TOKEN_FILENAME = "local_cli_token"
_TOKEN_BYTES = 32


def local_cli_token_path(gobby_home: Path | None = None) -> Path:
    """Return the local CLI token path under the active Gobby home."""
    return (gobby_home or default_gobby_home()) / LOCAL_CLI_TOKEN_FILENAME


def ensure_local_cli_token(gobby_home: Path | None = None) -> str:
    """Create or reuse the stable local CLI token."""
    path = local_cli_token_path(gobby_home)
    existing = read_local_cli_token(gobby_home)
    if existing:
        return existing

    token = secrets.token_urlsafe(_TOKEN_BYTES)
    _write_token(path, token)
    return token


def read_local_cli_token(gobby_home: Path | None = None) -> str | None:
    """Read the local CLI token, returning None when it is absent or empty."""
    path = local_cli_token_path(gobby_home)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not token:
        return None
    _chmod_token(path)
    return token


def _write_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(path.parent)
    handle, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(f"{token}\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, path)
        _chmod_token(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _chmod_token(path: Path) -> None:
    if os.name == "nt":
        return
    path.chmod(0o600)


def _chmod_private_dir(path: Path) -> None:
    if os.name == "nt":
        return
    path.chmod(0o700)
