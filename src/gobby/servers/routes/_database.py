"""Database helpers for HTTP route modules."""

from __future__ import annotations

from typing import Any, cast

from fastapi import HTTPException

from gobby.storage.hub.protocol import HubDatabase

_REQUIRED_HUB_DATABASE_METHODS = (
    "execute",
    "fetchone",
    "fetchall",
    "transaction",
    "transaction_immediate",
)


def require_hub_database(db: Any) -> HubDatabase:
    """Return a backend-neutral hub database adapter or raise HTTP 503."""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    if any(not callable(getattr(db, method, None)) for method in _REQUIRED_HUB_DATABASE_METHODS):
        raise HTTPException(status_code=503, detail="Database not available")
    return cast(HubDatabase, db)
