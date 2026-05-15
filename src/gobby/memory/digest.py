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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Protocol

from gobby.utils.json_helpers import extract_json_object

logger = logging.getLogger(__name__)

_LIFECYCLE_CMDS = ("/clear", "/exit", "/compact")
_MAX_SESSION_TITLE_LENGTH = 80
_TITLE_LINE_CLEANUP_RE = re.compile(r"^\s*(?:[-*+>#]+|\d+[.)])\s*")
_TITLE_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_TITLE_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_TITLE_LEADING_PHRASE_RE = re.compile(
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|"
    r"help\s+me\s+(?:to\s+)?|i(?:'d| would)?\s+like\s+to\s+|"
    r"i\s+want\s+to\s+|need\s+to\s+|we\s+need\s+to\s+|"
    r"let'?s\s+|can\s+we\s+|could\s+we\s+)",
    re.IGNORECASE,
)
_TITLE_BREAK_RE = re.compile(r"(?<=[.!?])\s+|[:;]\s+|\s+[/-]\s+")
_TITLE_ORCHESTRATION_BOILERPLATE_RE = re.compile(
    r"^a previous agent produced the plan below\b",
    re.IGNORECASE,
)
_TEMPLATE_PLACEHOLDER_RE = re.compile(
    r"^\[?\s*(?:"
    r"\d+\s*-\s*\d+\s+word\s+session\s+title(?:\s+reflecting\s+current\s+work)?|"
    r"accurate\s+summary\s+of\s+the\s+full\s+turn\s+with\s+user\s+request\s*\+\s*"
    r"agent\s+response"
    r")\s*\]?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _TurnRecord:
    turn_markdown: str
    title_candidate: str | None


class SessionTitlePolicy(Protocol):
    title: str | None
    title_source: str | None


async def memory_sync_import(memory_sync_manager: Any) -> dict[str, Any]:
    """Import memories from filesystem.

    Args:
        memory_sync_manager: The memory sync manager instance

    Returns:
        Dict with imported count or error
    """
    if not memory_sync_manager:
        return {"error": "Memory Sync Manager not available"}

    count = await memory_sync_manager.import_from_files()
    logger.info(f"Memory sync import: {count} memories imported")
    return {"imported": {"memories": count}}


async def memory_sync_export(
    memory_sync_manager: Any, project_id: str | None = None
) -> dict[str, Any]:
    """Export memories to filesystem.

    Args:
        memory_sync_manager: The memory sync manager instance
        project_id: Optional project to scope export to.

    Returns:
        Dict with exported count or error
    """
    if not memory_sync_manager:
        return {"error": "Memory Sync Manager not available"}

    count = await memory_sync_manager.export_to_files(project_id=project_id)
    logger.info(f"Memory sync export: {count} memories exported")
    return {"exported": {"memories": count}}


async def _read_last_turn_from_transcript(transcript_path: str, source: str) -> tuple[str, str]:
    """Read the last user prompt and assistant response from a transcript file.

    Args:
        transcript_path: Path to the JSONL transcript file
        source: CLI source (claude, gemini, qwen, codex, etc.)

    Returns:
        Tuple of (prompt_text, response_text). Empty strings if not found.
    """
    transcript_file = Path(transcript_path)
    if not transcript_file.exists():
        return "", ""

    try:
        from gobby.sessions.transcripts import get_parser

        parser = get_parser(source)

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
            if msg["role"] == "user":
                prompt_text = msg["content"]
            elif msg["role"] == "assistant":
                response_text = msg["content"]

        return prompt_text, response_text
    except Exception as e:
        logger.warning(f"Failed to read transcript {transcript_path}: {e}")
        return "", ""


async def _read_undigested_turns(
    transcript_path: str, source: str, digested_count: int, max_turns: int = 50, num_pairs: int = 50
) -> list[tuple[str, str]]:
    """Read user/assistant pairs from transcript that haven't been digested yet.

    Uses extract_turns_since_clear() to respect /clear boundaries, then
    extract_last_messages() to get all pairs from the current segment.
    Returns only pairs after digested_count.

    Args:
        transcript_path: Path to the JSONL transcript file
        source: CLI source (claude, gemini, qwen, codex, etc.)
        digested_count: Number of pairs already digested

    Returns:
        List of (prompt, response) tuples for undigested exchanges.
        Empty list if nothing new to digest.
    """
    transcript_file = Path(transcript_path)
    if not transcript_file.exists():
        return []

    try:
        from gobby.sessions.transcripts import get_parser

        parser = get_parser(source)
        import asyncio

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
            return []

        # Get current conversation segment (respects /clear boundaries)
        segment = parser.extract_turns_since_clear(turns, max_turns=max_turns)
        if not segment:
            return []

        # Extract all user/assistant messages from the segment
        messages = parser.extract_last_messages(segment, num_pairs=num_pairs)
        if not messages:
            return []

        # Pair messages into (prompt, response) tuples
        pairs: list[tuple[str, str]] = []
        current_prompt = ""
        for msg in messages:
            if msg["role"] == "user":
                # Consecutive user message means previous had no response (interrupted)
                if current_prompt:
                    pairs.append((current_prompt, ""))
                current_prompt = msg["content"]
            elif msg["role"] == "assistant":
                pairs.append((current_prompt or "", msg["content"]))
                current_prompt = ""
        # Trailing user message without response
        if current_prompt:
            pairs.append((current_prompt, ""))

        # Filter out lifecycle commands
        pairs = [
            (p, r)
            for p, r in pairs
            if not any(
                p.strip().lower() == c or p.strip().lower().startswith(c + " ")
                for c in _LIFECYCLE_CMDS
            )
        ]

        if not pairs:
            return []

        # Return undigested pairs
        if digested_count < len(pairs):
            return pairs[digested_count:]

        # Transcript has fewer pairs than digested (e.g., /clear reset) —
        # fall back to the last pair so we don't lose the current exchange
        logger.debug(
            f"Undigested turns fallback: digested_count={digested_count} >= len(pairs)={len(pairs)}. Returning last pair.",
        )
        return [pairs[-1]]

    except Exception as e:
        logger.warning(f"Failed to read undigested turns from {transcript_path}: {e}")
        return []


def _get_next_turn_number(previous_digest: str | None) -> int:
    """Parse existing digest to determine the next turn number.

    Args:
        previous_digest: The existing digest_markdown content

    Returns:
        Next turn number (1-based)
    """
    if not previous_digest:
        return 1

    # Find all "### Turn N" headers
    turn_numbers = re.findall(r"^### Turn (\d+)", previous_digest, re.MULTILINE)
    if not turn_numbers:
        return 1

    return max(int(n) for n in turn_numbers) + 1


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


def _build_title_synthesis_prompt(digest_markdown: str) -> str:
    """Build the title synthesis prompt inline (fallback when DB prompts unavailable)."""
    return (
        "Given a session's turn-by-turn digest, produce a 3-5 word title\n"
        "reflecting the current focus of the session.\n\n"
        f"## Session Digest\n{digest_markdown}\n\n"
        "Output only the title, nothing else."
    )


def _coerce_prompt_text(prompt_text: Any) -> str:
    """Normalize prompt text from string or multimodal blocks into plain text."""
    if isinstance(prompt_text, str):
        return prompt_text
    if not isinstance(prompt_text, list):
        return str(prompt_text or "")

    parts: list[str] = []
    for block in prompt_text:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
            continue
        if isinstance(block.get("content"), str):
            parts.append(block["content"])
    return "\n".join(part for part in parts if part)


def _truncate_title(title: str, limit: int = _MAX_SESSION_TITLE_LENGTH) -> str:
    """Clamp a title without cutting through a word when possible."""
    title = title.strip()
    if len(title) <= limit:
        return title

    words = title.split()
    truncated_words: list[str] = []
    current_length = 0
    for word in words:
        next_length = len(word) if not truncated_words else current_length + 1 + len(word)
        if next_length > limit:
            break
        truncated_words.append(word)
        current_length = next_length

    if truncated_words:
        return " ".join(truncated_words)
    return title[:limit].rstrip()


def _extract_markdown_h1_title(text: str) -> str | None:
    """Extract a markdown H1 title from wrapper prompts when present."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("# ") or line.startswith("##"):
            continue
        candidate = _TITLE_LINK_RE.sub(r"\1", line[2:]).replace("`", " ")
        candidate = re.sub(r"\s+", " ", candidate).strip(" \t\r\n.,:;!?-")
        if not candidate or candidate.startswith("/"):
            return None
        return _truncate_title(candidate)
    return None


def _build_heuristic_title(prompt_text: Any) -> str | None:
    """Derive a cheap bootstrap title from the first meaningful user prompt."""
    raw_text = _coerce_prompt_text(prompt_text)
    if not raw_text.strip():
        return None

    cleaned = _TITLE_CODE_BLOCK_RE.sub(" ", raw_text)
    cleaned = _TITLE_LINK_RE.sub(r"\1", cleaned)
    cleaned = cleaned.replace("`", " ")

    first_nonempty = next((line.strip() for line in cleaned.splitlines() if line.strip()), "")
    if _TITLE_ORCHESTRATION_BOILERPLATE_RE.match(first_nonempty):
        h1_title = _extract_markdown_h1_title(cleaned)
        return h1_title[0].upper() + h1_title[1:] if h1_title else None

    lines: list[str] = []
    for raw_line in cleaned.splitlines():
        line = _TITLE_LINE_CLEANUP_RE.sub("", raw_line).strip()
        if line:
            lines.append(line)

    if not lines:
        return None

    candidate = re.sub(r"\s+", " ", lines[0]).strip()
    if not candidate or candidate.startswith("/"):
        return None

    candidate = _TITLE_LEADING_PHRASE_RE.sub("", candidate)
    candidate = _TITLE_BREAK_RE.split(candidate, maxsplit=1)[0]
    candidate = candidate.strip(" \t\r\n.,:;!?-")
    if not candidate:
        return None

    words = candidate.split()
    if len(words) > 7:
        candidate = " ".join(words[:7])

    candidate = _truncate_title(candidate)
    if not candidate or len(candidate) < 2:
        return None

    return candidate[0].upper() + candidate[1:]


def _should_update_digest_title(session: SessionTitlePolicy) -> bool:
    """Return whether digest-owned title generation may update this session title."""
    existing_title = str(session.title or "").strip()
    raw_source = session.title_source
    title_source = str(raw_source or "").strip().lower()

    if title_source == "manual":
        return False
    if existing_title and raw_source is None:
        return False
    if not existing_title:
        return True
    return title_source in {"heuristic", "llm"} or not title_source


def _normalize_title_candidate(value: Any) -> str | None:
    """Validate and normalize an LLM-proposed title candidate."""
    if not isinstance(value, str):
        return None
    title = value.strip().strip('"').strip("'")
    title = _truncate_title(title)
    if _is_template_placeholder(title):
        return None
    return title or None


def _is_template_placeholder(value: str) -> bool:
    """Return True for prompt-template placeholders echoed by the LLM."""
    return bool(_TEMPLATE_PLACEHOLDER_RE.fullmatch(value.strip()))


async def bootstrap_session_title(
    session_manager: Any,
    session_id: str,
    prompt_text: Any,
) -> str | None:
    """Set a local heuristic title from the first meaningful prompt."""
    if not session_manager or not session_id:
        return None

    session = session_manager.get(session_id)
    if session is None:
        return None

    existing_title = str(getattr(session, "title", "") or "").strip()
    if existing_title:
        return None

    title = _build_heuristic_title(prompt_text)
    if not title:
        return None

    updated = session_manager.update_title(
        session_id,
        title,
        title_source="heuristic",
    )
    if updated is None:
        return None

    logger.info("Bootstrapped heuristic title for session %s", session_id)
    return title


async def _resolve_undigested_pairs(
    session: Any,
    prompt_text: str | None,
    session_id: str,
    max_turns: int = 50,
    num_pairs: int = 50,
) -> tuple[list[tuple[str, str]], str] | None:
    """Resolve undigested turn pairs from transcript or prompt_text.

    Returns:
        Tuple of (pairs, input_hash) or None if no content to digest.
    """
    undigested_pairs: list[tuple[str, str]] = []

    if session.transcript_path:
        previous_digest = getattr(session, "digest_markdown", None) or ""
        digested_count = _get_next_turn_number(previous_digest) - 1
        undigested_pairs = await _read_undigested_turns(
            session.transcript_path,
            session.source,
            digested_count,
            max_turns=max_turns,
            num_pairs=num_pairs,
        )

    if not undigested_pairs:
        user_prompt = prompt_text or ""
        if not user_prompt:
            logger.debug(f"build_turn_and_digest: No turn content for session {session_id}")
            return None
        _stripped = user_prompt.strip()
        if any(
            _stripped.lower() == c or _stripped.lower().startswith(c + " ") for c in _LIFECYCLE_CMDS
        ):
            return None
        undigested_pairs = [(user_prompt, "")]

    combined_content = "||".join(f"{p}||{r}" for p, r in undigested_pairs)
    input_hash = hashlib.sha256(combined_content.encode()).hexdigest()[:16]
    if session.last_digest_input_hash == input_hash:
        logger.debug(
            f"build_turn_and_digest: Skipping duplicate digest for session {session_id} (hash={input_hash})",
        )
        return None

    return undigested_pairs, input_hash


async def _build_turn_record(
    provider: Any,
    model: str | None,
    undigested_pairs: list[tuple[str, str]],
    db: Any | None = None,
) -> _TurnRecord:
    """Build and validate turn record JSON via LLM from undigested pairs."""
    max_prompt_chars = 4000
    max_response_chars = 8000

    if len(undigested_pairs) == 1:
        truncated_prompt = undigested_pairs[0][0][:max_prompt_chars]
        truncated_response = undigested_pairs[0][1][:max_response_chars]
    else:
        per_prompt = max_prompt_chars // len(undigested_pairs)
        per_response = max_response_chars // len(undigested_pairs)
        parts = []
        for i, (p, r) in enumerate(undigested_pairs, 1):
            parts.append(f"## Exchange {i}\nUser: {p[:per_prompt]}\nAgent: {r[:per_response]}")
        truncated_prompt = "\n\n".join(parts)
        truncated_response = ""

    try:
        from gobby.prompts.loader import PromptLoader

        loader = PromptLoader(db=db)
        turn_prompt = loader.render(
            "memory/turn_record",
            {"prompt_text": truncated_prompt, "response_text": truncated_response},
        )
    except Exception:
        turn_prompt = _build_turn_record_prompt(truncated_prompt, truncated_response)

    response_text = await provider.generate_text(
        turn_prompt,
        model=model,
        caller="memory.turn_record",
    )
    return _parse_turn_record_response(str(response_text), len(undigested_pairs))


def _parse_turn_record_response(response_text: str, exchange_count: int) -> _TurnRecord:
    """Parse the strict JSON contract for memory.turn_record responses."""
    data = extract_json_object(response_text)
    if data is None:
        _raise_turn_record_contract_error(
            "invalid or missing JSON object", response_text, exchange_count
        )

    turn_markdown = data.get("turn_markdown")
    if not isinstance(turn_markdown, str) or not turn_markdown.strip():
        _raise_turn_record_contract_error(
            "missing or empty turn_markdown",
            response_text,
            exchange_count,
        )
    turn_markdown = turn_markdown.strip()
    if _is_template_placeholder(turn_markdown):
        _raise_turn_record_contract_error(
            "placeholder turn_markdown",
            response_text,
            exchange_count,
        )

    raw_title_candidate = data.get("title_candidate")
    if not isinstance(raw_title_candidate, str) or not raw_title_candidate.strip():
        _raise_turn_record_contract_error(
            "missing or invalid title_candidate",
            response_text,
            exchange_count,
        )

    title_candidate = _normalize_title_candidate(raw_title_candidate)

    return _TurnRecord(turn_markdown=turn_markdown, title_candidate=title_candidate)


def _raise_turn_record_contract_error(
    reason: str, response_text: str, exchange_count: int
) -> NoReturn:
    logger.warning(
        "memory.turn_record contract failed: %s (response_chars=%d, exchanges=%d)",
        reason,
        len(response_text),
        exchange_count,
    )
    raise ValueError(f"memory.turn_record returned invalid JSON contract: {reason}")


async def _synthesize_title(
    provider: Any,
    model: str | None,
    updated_digest: str,
    session_id: str,
    session_manager: Any,
    session: Any,
    db: Any | None = None,
    llm_service: Any | None = None,
    digest_config: Any | None = None,
) -> str | None:
    """Synthesize session title from digest via LLM and update tmux window.

    When *llm_service* and *digest_config* are supplied, uses
    ``call_feature`` for tier-based fallback.  Otherwise falls back to
    the legacy ``provider.generate_text`` path.
    """
    if not _should_update_digest_title(session):
        return None

    try:
        from gobby.prompts.loader import PromptLoader

        loader = PromptLoader(db=db)
        title_prompt = loader.render(
            "memory/title_synthesis",
            {"digest_markdown": updated_digest},
        )
    except Exception:
        title_prompt = _build_title_synthesis_prompt(updated_digest)

    # Prefer call_feature for tier-based fallback when available.
    llm_timeout = getattr(digest_config, "timeout", 30) if digest_config is not None else 30
    if (
        llm_service is not None
        and digest_config is not None
        and hasattr(llm_service, "call_feature")
    ):
        title = await asyncio.wait_for(
            llm_service.call_feature(
                digest_config,
                title_prompt,
                caller="memory.title_synthesis",
            ),
            llm_timeout,
        )
    else:
        title = await asyncio.wait_for(
            provider.generate_text(
                title_prompt,
                model=model,
                caller="memory.title_synthesis",
            ),
            llm_timeout,
        )

    title_str = _normalize_title_candidate(title)
    if title_str:
        updated_session = session_manager.update_title(
            session_id,
            title_str,
            title_source="llm",
        )
        if updated_session is None:
            return None
        return title_str
    return None


async def build_turn_and_digest(
    memory_manager: Any,
    session_manager: Any,
    session_id: str,
    prompt_text: str | None = None,
    llm_service: Any | None = None,
    db: Any | None = None,
    config: Any | None = None,
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
        config: DaemonConfig for digest provider/model selection

    Returns:
        Dict with turn_num and pipeline results, or None if skipped
    """
    if not memory_manager or not memory_manager.config.enabled:
        logger.debug(
            "build_turn_and_digest: skipped — memory_manager missing or disabled "
            f"(session_id={session_id})"
        )
        return None

    if not llm_service:
        logger.debug(f"build_turn_and_digest: skipped — no llm_service (session_id={session_id})")
        return None

    # Check DigestConfig.enabled
    digest_config = getattr(config, "digest", None) if config else None
    if digest_config and not digest_config.enabled:
        logger.debug(
            f"build_turn_and_digest: skipped — digest config disabled (session_id={session_id})"
        )
        return None

    try:
        # 1. Get session
        session = session_manager.get(session_id) if session_manager else None
        if not session:
            logger.warning(f"build_turn_and_digest: Session {session_id} not found")
            return None

        title_source = str(getattr(session, "title_source", "") or "").strip().lower()
        needs_title_recovery = not bool(str(getattr(session, "title", "") or "").strip())
        needs_title_recovery = needs_title_recovery or title_source == "heuristic"
        existing_digest = getattr(session, "digest_markdown", None) or ""

        # 2. Resolve undigested pairs
        max_turns = getattr(digest_config, "max_turns", 50) if digest_config else 50
        num_pairs = getattr(digest_config, "num_pairs", 50) if digest_config else 50
        resolved = await _resolve_undigested_pairs(
            session, prompt_text, session_id, max_turns, num_pairs
        )
        if resolved is None and (not needs_title_recovery or not existing_digest):
            return None

        # 3. Resolve LLM provider/model
        if digest_config:
            try:
                provider, model, _ = llm_service.get_provider_for_feature(digest_config)
            except Exception:
                provider = llm_service.get_default_provider()
                model = None
        else:
            provider = llm_service.get_default_provider()
            model = None

        if resolved is None:
            try:
                title = await _synthesize_title(
                    provider,
                    model,
                    existing_digest,
                    session_id,
                    session_manager,
                    session,
                    db,
                    llm_service=llm_service,
                    digest_config=digest_config,
                )
            except Exception as e:
                logger.warning(f"build_turn_and_digest: Title synthesis failed: {e}")
                return None

            if not title:
                return None

            return {
                "title": title,
                "title_only": True,
                "digest_length": len(existing_digest),
            }

        undigested_pairs, input_hash = resolved

        # 4. Build turn record via LLM
        turn_record = await _build_turn_record(provider, model, undigested_pairs, db)
        last_turn = turn_record.turn_markdown

        # 5. Prepare digest/title state after validating the LLM JSON contract.
        previous_digest = getattr(session, "digest_markdown", None) or ""
        turn_num = _get_next_turn_number(previous_digest)
        entry = f"### Turn {turn_num}\n{last_turn}"
        updated_digest = f"{previous_digest}\n\n{entry}" if previous_digest else entry

        digest_title: str | None = None
        title_changed = False
        if turn_record.title_candidate and _should_update_digest_title(session):
            existing_title = str(getattr(session, "title", "") or "").strip()
            existing_title_source = str(getattr(session, "title_source", "") or "").strip().lower()
            digest_title = turn_record.title_candidate
            title_changed = existing_title != digest_title or existing_title_source != "llm"
            updated_session = session_manager.update_title(
                session_id,
                digest_title,
                title_source="llm",
            )
            if updated_session is None:
                raise RuntimeError("failed to update session title")

        # 6. Persist digest state only after contract validation and any title update succeed.
        session_manager.update_last_turn_markdown(session_id, last_turn)
        session_manager.update_digest_markdown(session_id, updated_digest)
        session_manager.update_last_digest_input_hash(session_id, input_hash)

        logger.info(
            f"build_turn_and_digest: Turn {turn_num} recorded ({len(last_turn)} chars) for session {session_id}",
        )

        result: dict[str, Any] = {
            "turn_num": turn_num,
            "turn_length": len(last_turn),
            "digest_length": len(updated_digest),
        }
        if digest_title and title_changed:
            result["title"] = digest_title

        return result

    except Exception as e:
        logger.error(
            f"build_turn_and_digest: Failed for session {session_id}: {e}",
            exc_info=True,
        )
        return {"error": str(e)}
