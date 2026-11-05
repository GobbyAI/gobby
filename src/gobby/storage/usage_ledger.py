"""Usage ledger reads over token_events, scoped to one session or one task (plan 3.1, #23601)."""

from __future__ import annotations

from typing import Any

from gobby.storage.hub.protocol import HubDatabase


class UsageLedgerStore:
    """Keyset-paged usage ledger for one session or one task."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def page(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Return one page of the ledger. Stub: implemented in the green step."""
        raise NotImplementedError("UsageLedgerStore.page")
