"""Shared dispatcher constants."""

from __future__ import annotations

MAX_ACTIVE_AGENTS = 10
DISPATCH_HOLDER = "dispatcher"
DISPATCH_TTL_SECONDS = 600
ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS = 30

__all__ = [
    "DISPATCH_HOLDER",
    "DISPATCH_TTL_SECONDS",
    "MAX_ACTIVE_AGENTS",
    "ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS",
]
