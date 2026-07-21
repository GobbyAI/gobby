from __future__ import annotations

import json
import logging

from gobby.sessions.summarize import SessionManagerProtocol, SessionSummaryConfigProtocol
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session

logger = logging.getLogger(__name__)

_SUMMARY_METADATA_RECOMPUTE_ERRORS = (
    AttributeError,
    KeyError,
    OSError,
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
    from gobby.sessions.analyzer import TranscriptAnalyzer
    from gobby.sessions.summarize import (
        _build_summary_prompt_context,
        _digest_markdown_for_summary,
        _source_hash_payload,
        load_summary_prompt_template,
    )
    from gobby.sessions.summary_refresh import (
        coerce_digest_turn_count,
        digest_turn_count,
        source_context_hash,
    )
    from gobby.sessions.summary_validity import is_summary_markdown_valid
    from gobby.sessions.workspace_context import enrich_git_context, resolve_session_workspace

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
        transcript_path = getattr(session, "transcript_path", None)
        cwd = resolve_session_workspace(session, transcript_path)
        handoff_ctx = TranscriptAnalyzer().extract_handoff_context([])
        await enrich_git_context(handoff_ctx, cwd)
        summary_context = await _build_summary_prompt_context(
            session=session,
            turns=[],
            handoff_ctx=handoff_ctx,
            db=db,
            session_manager=session_manager,
            project_path=str(cwd) if cwd is not None else None,
        )
        prompt_template = load_summary_prompt_template(
            path="handoff/session_end",
            session_summary_config=session_summary_config,
            db=db,
            session_manager=session_manager,
        )
        current_hash = source_context_hash(
            _source_hash_payload(
                session=session,
                digest_markdown=digest_markdown,
                summary_context=summary_context,
                prompt_template=prompt_template,
            )
        )
    except _SUMMARY_METADATA_RECOMPUTE_ERRORS as exc:
        logger.debug("Unable to recompute compact summary metadata: %s", exc, exc_info=True)
        return False

    return current_hash == stored_hash
