"""Task ownership helpers shared by storage transitions and updates."""

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._models import UNSET, MaybeUnset


def _session_exists(db: HubDatabase, session_id: str) -> bool:
    """Return whether the given session ID exists in storage."""
    return bool(db.fetchone("SELECT 1 FROM sessions WHERE id = %s", (session_id,)))


def _derive_claimed_by_session_id(
    db: HubDatabase,
    *,
    assignee: MaybeUnset[str | None] = UNSET,
    claimed_by_session_id: MaybeUnset[str | None] = UNSET,
) -> MaybeUnset[str | None]:
    """Project canonical ownership from explicit owner or session assignee.

    `claimed_by_session_id` is authoritative when explicitly provided.
    When only `assignee` is supplied, we mirror it into canonical ownership
    only if it resolves to a real session ID. This preserves compatibility-only
    assignee values such as web-chat conversation IDs.
    """
    if claimed_by_session_id is not UNSET:
        return claimed_by_session_id
    if assignee is UNSET:
        return UNSET
    if assignee is None:
        return None
    if isinstance(assignee, str) and _session_exists(db, assignee):
        return assignee
    return UNSET
