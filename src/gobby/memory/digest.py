"""Session digest pipeline — turn recording and boundary summaries.

Relocated from workflows/memory_actions.py as part of dead-code cleanup.
These functions handle the per-turn digest pipeline (build_turn_and_digest),
session boundary summaries, and sync operations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Protocol

from gobby.llm.base import LLMProviderCancellation
from gobby.memory.generation_schemas import TURN_RECORD_SCHEMA
from gobby.memory.shadow_relevance import judge_shadow_candidate_relevance
from gobby.memory.title_heuristics import (
    LIFECYCLE_CMDS,
    is_template_placeholder,
    normalize_title_candidate,
)
from gobby.sessions.summary_refresh import coerce_digest_turn_count, digest_turn_count
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions._title_defaults import DIGEST_TITLE_SOURCE, MANUAL_TITLE_SOURCE
from gobby.utils.injected_context import strip_injected_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TurnRecord:
    turn_markdown: str
    title_candidate: str


class SessionTitlePolicy(Protocol):
    title: str | None
    title_source: str | None


class _DigestPersistenceError(RuntimeError):
    """Raised when digest persistence would leave partial session state."""


@dataclass
class _DigestLockEntry:
    lock: asyncio.Lock
    users: int = 0


_DIGEST_TURN_SENTINEL_RE = re.compile(r"(?m)^[ \t]*<!-- gobby:digest-turn:(\d+) -->[ \t]*$")
_DIGEST_LOCKS: dict[str, _DigestLockEntry] = {}
_DIGEST_LOCKS_GUARD = threading.Lock()


@asynccontextmanager
async def _serialize_session_digest(session_id: str) -> AsyncIterator[None]:
    """Serialize digest reads and writes for one session."""
    with _DIGEST_LOCKS_GUARD:
        entry = _DIGEST_LOCKS.get(session_id)
        if entry is None:
            entry = _DigestLockEntry(lock=asyncio.Lock())
            _DIGEST_LOCKS[session_id] = entry
        entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        with _DIGEST_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0 and _DIGEST_LOCKS.get(session_id) is entry:
                del _DIGEST_LOCKS[session_id]


async def _run_sync_io(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run synchronous digest/session I/O without blocking the event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


def _parser_for_transcript(source: str | None, transcript_path: str) -> Any | None:
    """Resolve a parser or log why transcript processing is skipped."""
    from gobby.sessions.transcripts import get_parser

    if not str(source or "").strip():
        logger.warning("Skipping transcript %s: session source is missing", transcript_path)
        return None
    try:
        return get_parser(source)
    except ValueError as exc:
        logger.warning("Skipping transcript %s: %s", transcript_path, exc)
        return None


def _render_prompt_template(template: str, values: dict[str, str], db: HubDatabase) -> str:
    from gobby.prompts.loader import PromptLoader

    return PromptLoader(db=db).render(template, values)


def _extract_digest_pairs(parser: Any, turns: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Extract digestible pairs from a transcript slice."""
    if not turns:
        return []
    messages = parser.extract_last_messages(turns, num_pairs=max(1, len(turns)))
    messages = [
        {**msg, "content": stripped}
        for msg in messages
        if (stripped := strip_injected_context(str(msg["content"]))).strip()
    ]

    pairs: list[tuple[str, str]] = []
    current_prompt = ""
    for msg in messages:
        if msg["role"] == "user":
            if current_prompt:
                pairs.append((current_prompt, ""))
            current_prompt = msg["content"]
        elif msg["role"] == "assistant":
            pairs.append((current_prompt or "", msg["content"]))
            current_prompt = ""
    if current_prompt:
        pairs.append((current_prompt, ""))

    def _is_lifecycle_prompt(prompt: str) -> bool:
        normalized = " ".join(re.sub(r"<[^>]+>", "", prompt).lower().split())
        return any(
            normalized == command or normalized.startswith(command + " ")
            for command in LIFECYCLE_CMDS
        )

    return [(prompt, response) for prompt, response in pairs if not _is_lifecycle_prompt(prompt)]


def _provider_cancelled_result(session_id: str, exc: LLMProviderCancellation) -> dict[str, Any]:
    logger.info(
        "build_turn_and_digest: cancelled during provider shutdown for session %s: %s",
        session_id,
        exc,
    )
    return {"cancelled": True, "reason": str(exc)}


async def _read_last_turn_from_transcript(
    transcript_path: str, source: str | None
) -> tuple[str, str]:
    """Read the last user prompt and assistant response from a transcript file.

    Args:
        transcript_path: Path to the JSONL transcript file
    source: CLI source (claude, qwen, codex, etc.)

    Returns:
        Tuple of (prompt_text, response_text). Empty strings if not found.
    """
    transcript_file = Path(transcript_path)
    if not transcript_file.exists():
        return "", ""

    try:
        parser = _parser_for_transcript(source, transcript_path)
        if parser is None:
            return "", ""

        def _read_lines() -> list[str]:
            with open(transcript_file, encoding="utf-8") as f:
                return f.readlines()

        lines = await asyncio.to_thread(_read_lines)
        turns: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if line:
                turns.append(json.loads(line))

        if not turns:
            return "", ""

        # Extract last user/assistant pair
        messages = parser.extract_last_messages(turns, num_pairs=1)
        prompt_text = ""
        response_text = ""
        for msg in messages:
            content = strip_injected_context(str(msg["content"]))
            if msg["role"] == "user":
                prompt_text = content
            elif msg["role"] == "assistant":
                response_text = content

        return prompt_text, response_text
    except Exception as e:
        logger.warning("Failed to read transcript %s: %s", transcript_path, e)
        return "", ""


async def _read_undigested_turns(
    transcript_path: str,
    source: str | None,
    digested_pair_index: int,
    num_pairs: int = 50,
    *,
    prior_turn_only: bool = False,
) -> tuple[list[tuple[str, str]], int]:
    """Read user/assistant pairs from transcript that haven't been digested yet.

    Uses extract_turns_since_clear() to respect /clear boundaries, then
    extract_last_messages() to get all pairs from the current segment.
    The persisted cursor counts digestible pairs across all transcript segments,
    while returned content remains restricted to the current segment.

    Args:
        transcript_path: Path to the JSONL transcript file
    source: CLI source (claude, qwen, codex, etc.)
        digested_pair_index: Number of pairs already digested
        num_pairs: Maximum pairs to consume in this digest pass
        prior_turn_only: For Codex turn-start catch-up, exclude the active turn

    Returns:
        Tuple of the undigested pair batch and the next persisted pair index.
    """
    transcript_file = Path(transcript_path)
    if not transcript_file.exists():
        return [], digested_pair_index

    try:
        parser = _parser_for_transcript(source, transcript_path)
        if parser is None:
            return [], digested_pair_index

        def _read_lines() -> list[str]:
            with open(transcript_file, encoding="utf-8") as f:
                return f.readlines()

        lines = await asyncio.to_thread(_read_lines)
        turns: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if line:
                turns.append(json.loads(line))

        if not turns:
            return [], digested_pair_index

        # Get current conversation segment (respects /clear boundaries)
        segment = parser.extract_turns_since_clear(turns, max_turns=None)
        if not segment:
            return [], digested_pair_index

        segment_turn_offset = len(turns) - len(segment)
        if prior_turn_only:
            segment = _prior_codex_turns(segment, source)
            if not segment:
                return [], digested_pair_index

        pairs = _extract_digest_pairs(parser, segment)
        if not pairs:
            return [], digested_pair_index

        # Parsers return the active transcript suffix, but may sanitize records
        # in that suffix (for example Claude removes orphaned tool results).
        # Content equality therefore cannot identify the raw prefix reliably;
        # the preserved turn count is the stable boundary coordinate.
        prefix_turns = turns[:segment_turn_offset] if segment_turn_offset >= 0 else []
        segment_pair_offset = len(_extract_digest_pairs(parser, prefix_turns))
        start_index = digested_pair_index - segment_pair_offset
        if start_index < 0 or start_index > len(pairs):
            logger.debug(
                "Resetting digest cursor to active transcript segment: index=%s offset=%s pairs=%s",
                digested_pair_index,
                segment_pair_offset,
                len(pairs),
            )
            start_index = 0

        batch = pairs[start_index : start_index + num_pairs]
        return batch, segment_pair_offset + start_index + len(batch)

    except Exception as e:
        logger.warning("Failed to read undigested turns from %s: %s", transcript_path, e)
        return [], digested_pair_index


def _prior_codex_turns(
    segment: list[dict[str, Any]],
    source: str | None,
) -> list[dict[str, Any]]:
    """Return records before the active Codex turn, or an empty list when unavailable."""
    if (source or "").lower() != "codex":
        return []

    def is_task_started(record: dict[str, Any]) -> bool:
        payload = record.get("payload")
        return (
            record.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "task_started"
        )

    current_turn_start = next(
        (index for index in range(len(segment) - 1, -1, -1) if is_task_started(segment[index])),
        None,
    )
    return segment[:current_turn_start] if current_turn_start is not None else []


def _get_next_turn_number(previous_digest: str | None) -> int:
    """Get the next display turn from sanitized internal sentinels."""
    if not previous_digest:
        return 1

    turn_numbers = _DIGEST_TURN_SENTINEL_RE.findall(previous_digest)
    return max((int(number) for number in turn_numbers), default=0) + 1


def _sanitize_turn_markdown(turn_markdown: str) -> str:
    """Remove reserved sentinels so model output cannot forge digest state."""
    return _DIGEST_TURN_SENTINEL_RE.sub("", turn_markdown).strip()


def _build_turn_record_prompt(prompt_text: str, response_text: str) -> str:
    """Build the turn record prompt inline (fallback when DB prompts unavailable)."""
    return (
        "Given a conversation turn, produce a strict JSON object.\n\n"
        f"## User Prompt\n{prompt_text}\n\n"
        f"## Agent Response\n{response_text}\n\n"
        "## Instructions\n"
        "Return only valid JSON with exactly these string fields:\n"
        "- turn_markdown: non-empty markdown record of this turn in chronological order\n"
        "- title_candidate: concise 3-5 word session title candidate\n\n"
        "The title_candidate must describe the actual work, not command syntax. "
        "If the user prompt begins with a router or skill command such as "
        "`/gobby coderabbit`, `$gobby coderabbit`, `/help`, or `$skill`, ignore "
        "that command prefix and title the trailing task text or the work the "
        "agent performed. Use plain words only: no dates, timestamps, session or "
        "task refs, provider names, emoji, tree glyphs, bullets, or decorative "
        "punctuation. Never return a title that starts with `/` or `$`.\n\n"
        "turn_markdown must cover:\n"
        "- What the user asked or requested\n"
        "- What the agent found, decided, or accomplished\n"
        "- Each tool used and its purpose (file reads, edits, searches, commands)\n"
        "- Files created, modified, or deleted\n"
        "- Commits made (with refs)\n"
        "- Task operations (created, claimed, closed)\n"
        "- Key technical findings or decisions\n\n"
        "Write in concise past tense. Include specifics (file paths, function names,\n"
        "task refs like #N, commit SHAs). No filler. Target 200-400 words.\n\n"
        'Example: {"turn_markdown":"User asked...","title_candidate":"Digest JSON Titles"}'
    )


def _should_update_digest_title(session: SessionTitlePolicy) -> bool:
    """Return whether digest-owned title generation may update this session title."""
    title_source = str(getattr(session, "title_source", "") or "").strip().lower()
    return title_source != MANUAL_TITLE_SOURCE


async def _resolve_undigested_pairs(
    session: Any,
    prompt_text: str | None,
    session_id: str,
    num_pairs: int = 50,
    *,
    prior_turn_only: bool = False,
) -> tuple[list[tuple[str, str]], str, int] | None:
    """Resolve undigested turn pairs from transcript or prompt_text.

    Returns:
        Tuple of (pairs, input_hash, next_pair_index) or None if no content to digest.
    """
    undigested_pairs: list[tuple[str, str]] = []
    raw_pair_index = getattr(session, "last_digested_pair_index", 0)
    pair_index = raw_pair_index if isinstance(raw_pair_index, int) and raw_pair_index >= 0 else 0
    next_pair_index = pair_index

    if session.transcript_path:
        undigested_pairs, next_pair_index = await _read_undigested_turns(
            session.transcript_path,
            session.source,
            pair_index,
            num_pairs=num_pairs,
            prior_turn_only=prior_turn_only,
        )

    if not undigested_pairs:
        if prior_turn_only:
            return None
        user_prompt = prompt_text or ""
        if not user_prompt:
            logger.debug("build_turn_and_digest: No turn content for session %s", session_id)
            return None
        _stripped = user_prompt.strip()
        if any(
            _stripped.lower() == c or _stripped.lower().startswith(c + " ") for c in LIFECYCLE_CMDS
        ):
            return None
        undigested_pairs = [(user_prompt, "")]
        next_pair_index = pair_index + 1

    start_pair_index = next_pair_index - len(undigested_pairs)
    combined_content = f"{start_pair_index}||" + "||".join(f"{p}||{r}" for p, r in undigested_pairs)
    input_hash = hashlib.sha256(combined_content.encode()).hexdigest()[:16]
    if session.last_digest_input_hash == input_hash:
        logger.debug(
            "build_turn_and_digest: Skipping duplicate digest for session %s (hash=%s)",
            session_id,
            input_hash,
        )
        return None

    return undigested_pairs, input_hash, next_pair_index


def _turn_record_source_texts(pairs: list[tuple[str, str]]) -> tuple[str, str]:
    """Return complete turn texts for the turn-record prompt."""
    if len(pairs) == 1:
        return pairs[0]
    parts = [
        f"## Exchange {index}\nUser: {prompt}\nAgent: {response}"
        for index, (prompt, response) in enumerate(pairs, 1)
    ]
    return "\n\n".join(parts), ""


async def _build_turn_record(
    llm_service: Any,
    digest_config: Any,
    undigested_pairs: list[tuple[str, str]],
    db: HubDatabase,
) -> _TurnRecord:
    """Build and validate turn record JSON via LLM from undigested pairs."""
    max_attempts = 3
    prompt_text, response_text = _turn_record_source_texts(undigested_pairs)

    try:
        turn_prompt = await _run_sync_io(
            _render_prompt_template,
            "memory/turn_record",
            {"prompt_text": prompt_text, "response_text": response_text},
            db,
        )
    except Exception:
        turn_prompt = _build_turn_record_prompt(prompt_text, response_text)

    last_error: ValueError | None = None
    for attempt in range(1, max_attempts + 1):
        prompt = turn_prompt
        if last_error is not None:
            prompt = (
                f"{turn_prompt}\n\n"
                "## Correction\n"
                f"Your previous response failed the JSON contract: {last_error}. "
                "Return only one valid JSON object with non-empty string fields "
                "`turn_markdown` and `title_candidate`."
            )

        response = await llm_service.call_json_feature(
            digest_config,
            prompt,
            json_schema=TURN_RECORD_SCHEMA,
            caller="memory.turn_record",
        )
        try:
            return _validate_turn_record_payload(response, len(undigested_pairs))
        except ValueError as exc:
            if not str(exc).startswith("memory.turn_record returned invalid JSON contract"):
                raise
            last_error = exc
            if attempt >= max_attempts:
                raise
            logger.warning(
                "memory.turn_record retrying after contract failure (attempt %d/%d): %s",
                attempt,
                max_attempts,
                exc,
            )

    raise RuntimeError("memory.turn_record retry loop exited unexpectedly")


def _validate_turn_record_payload(data: dict[str, Any], exchange_count: int) -> _TurnRecord:
    """Validate the strict JSON contract for memory.turn_record responses."""
    turn_markdown = data.get("turn_markdown")
    if not isinstance(turn_markdown, str) or not turn_markdown.strip():
        _raise_turn_record_contract_error(
            "missing or empty turn_markdown",
            data,
            exchange_count,
        )
    turn_markdown = turn_markdown.strip()
    if is_template_placeholder(turn_markdown):
        _raise_turn_record_contract_error(
            "placeholder turn_markdown",
            data,
            exchange_count,
        )

    raw_title_candidate = data.get("title_candidate")
    if not isinstance(raw_title_candidate, str) or not raw_title_candidate.strip():
        _raise_turn_record_contract_error(
            "missing or invalid title_candidate",
            data,
            exchange_count,
        )

    title_candidate = normalize_title_candidate(raw_title_candidate)
    if not title_candidate:
        _raise_turn_record_contract_error(
            f"empty normalized title_candidate (raw_title_candidate={raw_title_candidate!r})",
            data,
            exchange_count,
        )

    return _TurnRecord(turn_markdown=turn_markdown, title_candidate=title_candidate)


def _raise_turn_record_contract_error(
    reason: str, payload: dict[str, Any], exchange_count: int
) -> NoReturn:
    response_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    response_preview = response_text[:200]
    response_sha256 = hashlib.sha256(response_text.encode("utf-8")).hexdigest()
    logger.debug(
        "memory.turn_record malformed response "
        "(reason=%s, response_chars=%d, response_sha256=%s, exchanges=%d, preview=%r)",
        reason,
        len(response_text),
        response_sha256,
        exchange_count,
        response_preview,
    )
    raise ValueError(f"memory.turn_record returned invalid JSON contract: {reason}")


def _schedule_summary_refresh_if_stale(
    *,
    memory_manager: Any,
    session_manager: Any,
    session: Any,
    updated_digest: str,
    session_id: str,
    llm_service: Any,
    db: HubDatabase,
    config: Any,
) -> None:
    """Refresh archival summary after digest grows past its watermark."""
    session_summary_config = getattr(config, "session_summary", None) if config else None
    if session_summary_config is None or llm_service is None:
        return
    current = digest_turn_count(updated_digest)
    watermark = coerce_digest_turn_count(getattr(session, "summary_digest_turn_count", None)) or 0
    if current <= watermark:
        return
    from gobby.sessions.summarize import generate_session_summaries

    memory_manager.schedule_background_task(
        generate_session_summaries(
            session_id=session_id,
            session_manager=session_manager,
            llm_service=llm_service,
            session_summary_config=session_summary_config,
            db=db,
            set_handoff_ready=False,
        ),
        name=f"session-summary-refresh-{session_id}",
    )


async def _build_turn_and_digest_serialized(
    memory_manager: Any,
    session_manager: Any,
    session_id: str,
    prompt_text: str | None = None,
    llm_service: Any | None = None,
    db: HubDatabase | None = None,
    config: Any | None = None,
    prior_turn_only: bool = False,
) -> dict[str, Any] | None:
    """Build a detailed turn record, append to digest, and synthesize title.

    This is the core per-turn pipeline, fired after each agent response (stop event).
    It reads the last user/assistant exchange from the transcript, generates a structured
    turn record via LLM, appends it to the session's rolling digest, and synthesizes a title.

    Args:
        memory_manager: The memory manager instance
        session_manager: The session manager instance
        session_id: Platform session ID
        prompt_text: Optional user prompt (usually None for stop events, read from transcript)
        llm_service: LLM service for generation
        db: Database for prompt template loading
        config: DaemonConfig carrying the digest feature configuration
        prior_turn_only: Digest only the prior interrupted Codex turn

    Returns:
        Dict with turn_num and pipeline results, or None if skipped
    """
    if not memory_manager or not memory_manager.config.enabled:
        logger.debug(
            "build_turn_and_digest: skipped — memory_manager missing or disabled (session_id=%s)",
            session_id,
        )
        return None

    if not llm_service:
        logger.debug("build_turn_and_digest: skipped — no llm_service (session_id=%s)", session_id)
        return None

    # Reuse the daemon's hub handle when the caller did not pass one.
    if db is None:
        db = getattr(memory_manager, "db", None)
    if db is None:
        logger.warning(
            "build_turn_and_digest: skipped — no database available (session_id=%s)", session_id
        )
        return {"error": "database not available for prompt loading"}

    # Check DigestConfig.enabled
    digest_config = getattr(config, "digest", None) if config else None
    if digest_config and not digest_config.enabled:
        logger.debug(
            "build_turn_and_digest: skipped — digest config disabled (session_id=%s)", session_id
        )
        return None

    try:
        # 1. Get session
        session = await _run_sync_io(session_manager.get, session_id) if session_manager else None
        if not session:
            logger.warning("build_turn_and_digest: Session %s not found", session_id)
            return None

        # 2. Resolve undigested pairs
        num_pairs = getattr(digest_config, "num_pairs", 50) if digest_config else 50
        resolved = await _resolve_undigested_pairs(
            session,
            prompt_text,
            session_id,
            num_pairs,
            prior_turn_only=prior_turn_only,
        )
        if resolved is None:
            return None

        if digest_config is None:
            return {"error": "memory digest feature config not available"}

        undigested_pairs, input_hash, next_pair_index = resolved

        # 4. Build turn record via LLM
        turn_record = await _build_turn_record(llm_service, digest_config, undigested_pairs, db)
        last_turn = _sanitize_turn_markdown(turn_record.turn_markdown)
        if not last_turn:
            raise ValueError("turn_markdown contains only reserved digest sentinels")

        # 5. Prepare digest/title state after validating the LLM JSON contract.
        previous_digest = getattr(session, "digest_markdown", None) or ""
        turn_num = _get_next_turn_number(previous_digest)
        entry = f"<!-- gobby:digest-turn:{turn_num} -->\n### Turn {turn_num}\n{last_turn}"
        updated_digest = f"{previous_digest}\n\n{entry}" if previous_digest else entry

        digest_title: str | None = None
        title_changed = False
        persist_title: str | None = None
        persist_title_source: str | None = None
        if turn_record.title_candidate and _should_update_digest_title(session):
            existing_title = str(getattr(session, "title", "") or "").strip()
            existing_title_source = str(getattr(session, "title_source", "") or "").strip().lower()
            digest_title = turn_record.title_candidate
            title_changed = (
                existing_title != digest_title or existing_title_source != DIGEST_TITLE_SOURCE
            )
            if title_changed:
                persist_title = digest_title
                persist_title_source = DIGEST_TITLE_SOURCE

        # 6. Persist digest state only after contract validation succeeds.
        try:
            updated_session = await _run_sync_io(
                session_manager.persist_digest_state,
                session_id,
                last_turn_markdown=last_turn,
                digest_markdown=updated_digest,
                last_digest_input_hash=input_hash,
                last_digested_pair_index=next_pair_index,
                title=persist_title,
                title_source=persist_title_source,
            )
        except Exception as exc:
            raise _DigestPersistenceError(
                "Failed to persist session digest state for "
                f"{session_id}; aborting digest persistence to avoid partial state."
            ) from exc

        if updated_session is None:
            raise _DigestPersistenceError(
                "Failed to persist session digest state for "
                f"{session_id}; aborting digest persistence to avoid partial state."
            )

        logger.debug(
            "build_turn_and_digest: Turn %s recorded (%s chars) for session %s",
            turn_num,
            len(last_turn),
            session_id,
        )
        _schedule_summary_refresh_if_stale(
            memory_manager=memory_manager,
            session_manager=session_manager,
            session=updated_session,
            updated_digest=updated_digest,
            session_id=session_id,
            llm_service=llm_service,
            db=db,
            config=config,
        )

        result: dict[str, Any] = {
            "turn_num": turn_num,
            "turn_length": len(last_turn),
            "digest_length": len(updated_digest),
        }
        if digest_title and title_changed:
            result["title"] = digest_title

        # Poll durable recall rows after digest persistence. The manager retains
        # this work independently so digest completion does not await judging.
        memory_manager.schedule_background_task(
            judge_shadow_candidate_relevance(
                memory_manager=memory_manager,
                llm_service=llm_service,
                config=config,
                session_id=session_id,
            ),
            name=f"memory-shadow-judge-{session_id}",
        )

        return result

    except _DigestPersistenceError:
        raise
    except LLMProviderCancellation as e:
        return _provider_cancelled_result(session_id, e)
    except Exception as e:
        logger.exception(
            "build_turn_and_digest: Failed for session %s: %s",
            session_id,
            e,
        )
        return {"error": str(e)}


async def build_turn_and_digest(
    memory_manager: Any,
    session_manager: Any,
    session_id: str,
    prompt_text: str | None = None,
    llm_service: Any | None = None,
    db: HubDatabase | None = None,
    config: Any | None = None,
    prior_turn_only: bool = False,
) -> dict[str, Any] | None:
    """Build one digest turn and its title under a per-session serialization lock."""
    async with _serialize_session_digest(session_id):
        return await _build_turn_and_digest_serialized(
            memory_manager=memory_manager,
            session_manager=session_manager,
            session_id=session_id,
            prompt_text=prompt_text,
            llm_service=llm_service,
            db=db,
            config=config,
            prior_turn_only=prior_turn_only,
        )
