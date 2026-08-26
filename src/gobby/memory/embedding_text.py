"""The text a memory's vector is computed from."""

from __future__ import annotations

_RATIONALE_PREFIX = "\n\nWhy: "


def memory_embedding_text(content: str, rationale: str | None) -> str:
    """Return ``content`` with its rationale appended when one exists.

    The rationale states when a future session needs the memory, which is the
    signal a search query carries and the body often lacks; embedding both puts
    that signal on the vector (#21010). Every embedding of a stored memory --
    create, update, reindex, backfill, crossref, the duplicate scan, and the
    write-time ``similar_existing`` probe -- goes through this helper so the
    corpus and its probes share one representation.
    """
    why = (rationale or "").strip()
    return f"{content}{_RATIONALE_PREFIX}{why}" if why else content
