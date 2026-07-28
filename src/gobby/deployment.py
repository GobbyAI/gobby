"""Stable deployment identity derived from the resolved Gobby data root."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from gobby.paths import get_gobby_home


def deployment_token(data_root: Path | None = None) -> str:
    """Return the stable namespace shared by daemon lifecycles for one data root."""
    if data_root is None:
        return _default_deployment_token()
    root = data_root.expanduser().resolve()
    return hashlib.sha256(str(root).encode()).hexdigest()[:16]


@lru_cache(maxsize=1)
def _default_deployment_token() -> str:
    root = get_gobby_home().expanduser().resolve()
    return hashlib.sha256(str(root).encode()).hexdigest()[:16]


def deployment_advisory_key(purpose: str, *, token: str | None = None) -> int:
    """Derive a signed PostgreSQL advisory-lock key for one deployment purpose."""
    namespace = token or deployment_token()
    digest = hashlib.sha256(f"gobby:{namespace}:{purpose}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)
