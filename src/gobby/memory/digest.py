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
from typing import Any, NamedTuple, NoReturn, Protocol

from jinja2 import TemplateError

from gobby.llm.base import LLMProviderCancellation
from gobby.memory.generation_schemas import TURN_RECORD_SCHEMA
from gobby.memory.shadow_relevance import judge_shadow_candidate_relevance
from gobby.memory.synthetic_prompts import synthetic_body_reason
from gobby.memory.title_heuristics import (
    LIFECYCLE_CMDS,
    is_template_placeholder,
    normalize_title_candidate,
)
from gobby.sessions.summary_refresh import (
    DIGEST_TURN_SENTINEL_RE,
    coerce_digest_turn_count,
    digest_turn_count,
)
from gobby.sessions.transcripts.base import TranscriptReadError, decode_transcript_record
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions._title_defaults import DIGEST_TITLE_SOURCE, MANUAL_TITLE_SOURCE
from gobby.utils.injected_context import strip_injected_context

logger = logging.getLogger(__name__)

TRANSCRIPT_TAIL_RETRY_DELAY_SECONDS = 0.2


@dataclass(frozen=True)
class _TurnRecord:
    turn_markdown: str
    title_candidate: str


class DigestPair(NamedTuple):
    prompt: str
    response: str
    activity: str


class UndigestedBatch(NamedTuple):
    pairs: list[tuple[str, str]]
    next_pair_index: int
    tail_withheld: bool
    tail_pair: DigestPair | None


class ResolvedPairs(NamedTuple):
    pairs: list[tuple[str, str]]
    input_hash: str
    next_pair_index: int
    tail_withheld: bool
    tail_pair: DigestPair | None


class SessionTitlePolicy(Protocol):
    title: str | None
    title_source: str | None


class _DigestPersistenceError(RuntimeError):
    """Raised when digest persistence would leave partial session state."""


@dataclass
class _DigestLockEntry:
    lock: asyncio.Lock
    users: int = 0


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


def _extract_digest_pairs(parser: Any, turns: list[dict[str, Any]]) -> list[DigestPair]:
    """Extract digestible pairs from a transcript slice."""
    if not turns:
        return []
    messages = parser.extract_last_messages(
        turns,
        num_pairs=max(1, len(turns)),
        include_tool_activity=True,
    )
    messages = [
        {**msg, "content": stripped}
        for msg in messages
        if (stripped := strip_injected_context(str(msg["content"]))).strip()
    ]

    pairs: list[DigestPair] = []
    current_prompt = ""
    current_activity = ""
    for msg in messages:
        if msg["role"] == "user":
            if current_prompt:
                pairs.append(DigestPair(current_prompt, "", current_activity))
            current_prompt = msg["content"]
            current_activity = str(msg.get("tool_activity") or "")
        elif msg["role"] == "assistant":
            pairs.append(DigestPair(current_prompt or "", msg["content"], current_activity))
            current_prompt = ""
            current_activity = ""
    if current_prompt:
        pairs.append(DigestPair(current_prompt, "", current_activity))

    def _is_lifecycle_prompt(prompt: str) -> bool:
        normalized = " ".join(re.sub(r"<[^>]+>", "", prompt).lower().split())
        return any(
            normalized == command or normalized.startswith(command + " ")
            for command in LIFECYCLE_CMDS
        )

    def _is_synthetic_noise(prompt: str, response: str) -> bool:
        """Daemon-generated prompt with no agent response — nothing happened."""
        return not response.strip() and synthetic_body_reason(prompt) is not None

    return [
        pair
        for pair in pairs
        if not _is_lifecycle_prompt(pair.prompt)
        and not _is_synthetic_noise(pair.prompt, pair.response)
    ]


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
    catch_up: bool = False,
) -> UndigestedBatch:
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
        catch_up: Backlog catch-up outside turn end — exclude the active turn

    Returns:
        The composed undigested pairs, next cursor, and recoverable tail evidence.
    """
    transcript_file = Path(transcript_path)
    if not transcript_file.exists():
        return UndigestedBatch([], digested_pair_index, False, None)

    try:
        parser = _parser_for_transcript(source, transcript_path)
        if parser is None:
            return UndigestedBatch([], digested_pair_index, False, None)

        turns, tail_withheld = await _read_digest_records(transcript_file)

        if not turns:
            return UndigestedBatch([], digested_pair_index, tail_withheld, None)

        # Get current conversation segment (respects /clear boundaries)
        segment = parser.extract_turns_since_clear(turns, max_turns=None)
        if not segment:
            return UndigestedBatch([], digested_pair_index, tail_withheld, None)

        segment_turn_offset = len(turns) - len(segment)
        is_codex = (source or "").lower() == "codex"
        if catch_up and is_codex:
            # Codex transcripts carry an explicit task_started marker for the
            # active turn; everything before it is complete history.
            segment = _prior_codex_turns(segment, source)
            if not segment:
                return UndigestedBatch([], digested_pair_index, tail_withheld, None)

        extracted_pairs = _extract_digest_pairs(parser, segment)
        withheld_pair = extracted_pairs[-1] if tail_withheld and extracted_pairs else None
        digestible_pairs = extracted_pairs[:-1] if withheld_pair is not None else extracted_pairs
        if (
            catch_up
            and not is_codex
            and digestible_pairs
            and not digestible_pairs[-1].response.strip()
        ):
            # Without a turn marker, a trailing pair with no response is the
            # in-flight turn (or the just-submitted prompt); leave it for the
            # turn-end digest so the cursor never consumes an active turn.
            digestible_pairs = digestible_pairs[:-1]
        if not digestible_pairs:
            return UndigestedBatch([], digested_pair_index, tail_withheld, withheld_pair)

        # Parsers return the active transcript suffix, but may sanitize records
        # in that suffix (for example Claude removes orphaned tool results).
        # Content equality therefore cannot identify the raw prefix reliably;
        # the preserved turn count is the stable boundary coordinate.
        prefix_turns = turns[:segment_turn_offset] if segment_turn_offset >= 0 else []
        segment_pair_offset = len(_extract_digest_pairs(parser, prefix_turns))
        start_index = digested_pair_index - segment_pair_offset
        if start_index < 0 or start_index > len(digestible_pairs):
            logger.debug(
                "Resetting digest cursor to active transcript segment: index=%s offset=%s pairs=%s",
                digested_pair_index,
                segment_pair_offset,
                len(digestible_pairs),
            )
            start_index = 0

        selected_pairs = digestible_pairs[start_index : start_index + num_pairs]
        composed_pairs = [
            (
                pair.prompt,
                "\n\n".join(part for part in (pair.response, pair.activity) if part).strip(),
            )
            for pair in selected_pairs
        ]
        tail_pair = (
            withheld_pair if tail_withheld else (selected_pairs[-1] if selected_pairs else None)
        )
        return UndigestedBatch(
            composed_pairs,
            segment_pair_offset + start_index + len(selected_pairs),
            tail_withheld,
            tail_pair,
        )

    except TranscriptReadError:
        raise
    except Exception as e:
        logger.warning("Failed to read undigested turns from %s: %s", transcript_path, e)
        return UndigestedBatch([], digested_pair_index, False, None)


async def _read_digest_records(path: Path) -> tuple[list[dict[str, Any]], bool]:
    data = await asyncio.to_thread(path.read_bytes)
    records, tail_withheld = _decode_digest_records(data, path)
    if not tail_withheld:
        return records, False
    await asyncio.sleep(TRANSCRIPT_TAIL_RETRY_DELAY_SECONDS)
    data = await asyncio.to_thread(path.read_bytes)
    return _decode_digest_records(data, path)


def _decode_digest_records(data: bytes, path: Path) -> tuple[list[dict[str, Any]], bool]:
    records: list[dict[str, Any]] = []
    offset = 0
    lines = data.splitlines(keepends=True)
    for index, raw_record in enumerate(lines):
        if raw_record.strip():
            record = decode_transcript_record(
                raw_record,
                path=path,
                byte_offset=offset,
                line_number=index + 1,
                is_final=index == len(lines) - 1,
            )
            if record is None:
                return records, True
            records.append(record)
        offset += len(raw_record)
    return records, False


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

    turn_numbers = DIGEST_TURN_SENTINEL_RE.findall(previous_digest)
    return max((int(number) for number in turn_numbers), default=0) + 1


def _sanitize_turn_markdown(turn_markdown: str) -> str:
    """Remove reserved sentinels so model output cannot forge digest state."""
    return DIGEST_TURN_SENTINEL_RE.sub("", turn_markdown).strip()


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
        "The Agent Response may end with a `[tool activity]` ledger: one line per tool call in\n"
        "order, with the primary argument (file path, command, query, MCP server:tool and task\n"
        "ref) and ` ! failed:` annotations. Treat that ledger as the authoritative record of\n"
        "tools used, files created or modified, commands run, commits, and task operations;\n"
        "narration that contradicts it is wrong. A line with no annotation completed\n"
        "successfully — a bare test command line means those tests ran and passed; ` ! failed:`\n"
        "means the call failed; `(no result recorded)` means the call was still in flight when\n"
        "the turn ended.\n\n"
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
    catch_up: bool = False,
) -> ResolvedPairs | None:
    """Resolve undigested turn pairs from transcript or prompt_text.

    Returns:
        Resolved pairs and tail evidence, or None if no content needs digesting.
    """
    raw_pair_index = getattr(session, "last_digested_pair_index", 0)
    pair_index = raw_pair_index if isinstance(raw_pair_index, int) and raw_pair_index >= 0 else 0
    batch = UndigestedBatch([], pair_index, False, None)

    if session.transcript_path:
        batch = await _read_undigested_turns(
            session.transcript_path,
            session.source,
            pair_index,
            num_pairs=num_pairs,
            catch_up=catch_up,
        )

    undigested_pairs = batch.pairs
    next_pair_index = batch.next_pair_index
    tail_pair = batch.tail_pair
    if not undigested_pairs:
        if batch.tail_withheld:
            return ResolvedPairs([], "", next_pair_index, True, tail_pair)
        if catch_up:
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
        if synthetic_body_reason(_stripped) is not None:
            logger.debug(
                "build_turn_and_digest: Skipping synthetic prompt for session %s", session_id
            )
            return None
        undigested_pairs = [(user_prompt, "")]
        next_pair_index = pair_index + 1
        tail_pair = DigestPair(user_prompt, "", "")

    start_pair_index = next_pair_index - len(undigested_pairs)
    combined_content = f"{start_pair_index}||" + "||".join(f"{p}||{r}" for p, r in undigested_pairs)
    input_hash = hashlib.sha256(combined_content.encode()).hexdigest()[:16]
    if session.last_digest_input_hash == input_hash:
        logger.debug(
            "build_turn_and_digest: Skipping duplicate digest for session %s (hash=%s)",
            session_id,
            input_hash,
        )
        if batch.tail_withheld:
            return ResolvedPairs([], "", next_pair_index, True, tail_pair)
        return None

    return ResolvedPairs(
        undigested_pairs,
        input_hash,
        next_pair_index,
        batch.tail_withheld,
        tail_pair,
    )


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
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError, TemplateError):
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
    from gobby.sessions.summarize import refresh_session_summary_to_watermark

    memory_manager.schedule_background_task(
        refresh_session_summary_to_watermark(
            session_id=session_id,
            minimum_digest_turn_count=current,
            session_manager=session_manager,
            llm_service=llm_service,
            session_summary_config=session_summary_config,
            db=db,
        ),
        name=f"session-summary-refresh-{session_id}",
    )


async def _persist_digest_state(
    session_manager: Any,
    session_id: str,
    **values: Any,
) -> Any:
    """Keep the session lock until an in-flight persistence worker settles."""
    persist = asyncio.ensure_future(
        _run_sync_io(session_manager.persist_digest_state, session_id, **values)
    )
    try:
        return await asyncio.shield(persist)
    except asyncio.CancelledError:
        while not persist.done():
            try:
                await asyncio.wait({persist})
            except asyncio.CancelledError:
                continue
        if (exc := persist.exception()) is not None:
            logger.warning(
                "digest persistence failed for %s during cancellation",
                session_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        raise


async def _build_turn_and_digest_serialized(
    memory_manager: Any,
    session_manager: Any,
    session_id: str,
    prompt_text: str | None = None,
    llm_service: Any | None = None,
    db: HubDatabase | None = None,
    config: Any | None = None,
    catch_up: bool = False,
    *,
    withheld_capture: dict[str, Any] | None = None,
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
        catch_up: Drain a bounded undigested backlog batch, excluding the active turn

    Returns:
        Dict with turn_num and pipeline results, or None if skipped
    """
    tail_result: dict[str, Any] = {}
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
        if catch_up:
            num_pairs = getattr(digest_config, "catch_up_num_pairs", 5) if digest_config else 5
        else:
            num_pairs = getattr(digest_config, "num_pairs", 50) if digest_config else 50
        resolved = await _resolve_undigested_pairs(
            session,
            prompt_text,
            session_id,
            num_pairs,
            catch_up=catch_up,
        )
        if resolved is None:
            return None

        tail_pair_payload = resolved.tail_pair._asdict() if resolved.tail_pair is not None else None
        if withheld_capture is not None:
            withheld_capture.clear()
            withheld_capture.update(
                {
                    "tail_withheld": resolved.tail_withheld,
                    "withheld_pair": tail_pair_payload,
                }
            )
        if resolved.tail_withheld:
            tail_result = {
                "tail_withheld": True,
                "withheld_pair": tail_pair_payload,
            }
        if not resolved.pairs:
            return tail_result or None

        if digest_config is None:
            return {"error": "memory digest feature config not available", **tail_result}

        undigested_pairs = resolved.pairs
        input_hash = resolved.input_hash
        next_pair_index = resolved.next_pair_index

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
            updated_session = await _persist_digest_state(
                session_manager,
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
        if not resolved.tail_withheld:
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
        result.update(tail_result)

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
    except TranscriptReadError as e:
        logger.warning(
            "build_turn_and_digest: Corrupt transcript for session %s: %s",
            session_id,
            e,
        )
        return {"error": str(e), "error_kind": "transcript_read", **tail_result}
    except LLMProviderCancellation as e:
        return {**_provider_cancelled_result(session_id, e), **tail_result}
    except Exception as e:
        logger.exception(
            "build_turn_and_digest: Failed for session %s: %s",
            session_id,
            e,
        )
        return {"error": str(e), **tail_result}


async def build_turn_and_digest(
    memory_manager: Any,
    session_manager: Any,
    session_id: str,
    prompt_text: str | None = None,
    llm_service: Any | None = None,
    db: HubDatabase | None = None,
    config: Any | None = None,
    catch_up: bool = False,
    *,
    withheld_capture: dict[str, Any] | None = None,
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
            catch_up=catch_up,
            withheld_capture=withheld_capture,
        )
