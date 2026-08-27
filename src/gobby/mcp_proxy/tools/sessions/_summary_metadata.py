from __future__ import annotations

import json
import logging

from gobby.sessions.machine_scope import require_local_session_ownership
from gobby.sessions.summarize import SessionManagerProtocol, SessionSummaryConfigProtocol
from gobby.sessions.transcripts.base import TranscriptReadError
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session

logger = logging.getLogger(__name__)

_SUMMARY_METADATA_RECOMPUTE_ERRORS = (
    AttributeError,
    KeyError,
    OSError,
    TranscriptReadError,
    TypeError,
    ValueError,
    json.JSONDecodeError,
)


async def compact_summary_metadata_matches(
    *,
    session: Session,
    session_manager: SessionManagerProtocol,
    db: HubDatabase | None,
    session_summary_config: SessionSummaryConfigProtocol | None,
) -> bool:
    """Return whether cached summary metadata matches current compact source context."""
    require_local_session_ownership(session)

    from gobby.sessions.summarize import build_summary_source_context
    from gobby.sessions.summary_refresh import (
        coerce_digest_turn_count,
        digest_turn_count,
    )
    from gobby.sessions.summary_transcripts import _digest_markdown_for_summary
    from gobby.sessions.summary_validity import is_summary_markdown_valid

    if not is_summary_markdown_valid(getattr(session, "summary_markdown", None)):
        return False

    stored_hash = getattr(session, "summary_source_context_hash", None)
    if not isinstance(stored_hash, str) or not stored_hash.strip():
        return False

    digest_markdown = _digest_markdown_for_summary(session)
    current_count = digest_turn_count(digest_markdown)
    if current_count <= 0:
        return False
    previous_count = coerce_digest_turn_count(getattr(session, "summary_digest_turn_count", None))
    if previous_count != current_count:
        return False

    try:
        context = await build_summary_source_context(
            session,
            db=db,
            session_manager=session_manager,
            session_summary_config=session_summary_config,
        )
    except _SUMMARY_METADATA_RECOMPUTE_ERRORS as exc:
        logger.debug("Unable to recompute compact summary metadata: %s", exc, exc_info=True)
        return False

    return context is not None and context.source_hash == stored_hash
