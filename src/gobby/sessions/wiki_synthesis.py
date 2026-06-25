"""Session knowledge-synthesis (wiki) generation.

Companion to summary generation (:mod:`gobby.sessions.summarize`): hangs off the
tail of ``generate_session_summaries`` so every summary-producing path also
produces a concise, durable, cross-linked wiki source page synthesized from the
per-turn digest (NOT the raw transcript).

The summary is handoff-purposed (Current State / Files Changed / Next Steps);
the wiki page is durable knowledge (Summary / Key Claims / Key Quotes /
Connections / Contradictions). Different artifact, different prompt
(``wiki/source_page``), different owner columns (``sessions.wiki_*``) and
revision table (``session_wiki_revisions``).

Security: the synthesis can lift secrets from the digest verbatim (Key Quotes),
so the assembled page is redacted ONCE before any persistence. The DB column and
the mirror file are written from the same redacted body and cannot diverge.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from gobby.sessions.summary_context import _summary_context_db
from gobby.sessions.summary_refresh import (
    coerce_digest_turn_count,
    digest_turn_count,
    source_context_hash,
)

logger = logging.getLogger(__name__)

#: PromptLoader path for the wiki synthesis prompt (installed prompt record).
WIKI_PROMPT_PATH = "wiki/source_page"

#: Minimum structured digest turns before synthesis runs. Parity with the
#: summary skip threshold (lifecycle treats ``< 3`` turns as skip-worthy), so
#: "wiki never runs where summary wouldn't" holds literally.
WIKI_MIN_DIGEST_TURNS = 3

#: A knowledge page is always a full rebuild (no per-turn/delta mode).
WIKI_GENERATION_MODE = "full"

#: Ephemeral session sources that never warrant LLM-heavy synthesis. Mirrors the
#: skip policy in ``SessionLifecycleManager._process_pending_transcripts``.
_EPHEMERAL_SOURCES = ("pipeline", "cron")

#: Sections that must be present for a synthesis to count as valid output.
_REQUIRED_SECTIONS = ("Summary", "Key Claims")

_HEADING_RE = re.compile(
    r"^(?P<marks>#{1,6})\s+(?P<title>.*?)\s*(?:#+\s*)?$",
    re.MULTILINE,
)

_SUGGESTED_TAGS_RE = re.compile(
    r"<!--\s*suggested-tags:\s*(?P<tags>.*?)\s*-->",
    re.IGNORECASE | re.DOTALL,
)

# Redaction patterns applied once to the assembled page before persistence.
# Order matters: specific secrets first, then the home-directory path.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # OpenAI / Anthropic style keys: sk-..., sk-proj-..., sk-ant-...
    (re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{15,}"), "sk-<redacted>"),
    # GitHub tokens: ghp_, gho_, ghu_, ghs_, ghr_
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "gh<redacted-token>"),
    # AWS access key IDs
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA<redacted>"),
    # Bearer / Authorization tokens
    (
        re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{16,}"),
        r"\1 <redacted>",
    ),
    # Generic secret-bearing key/value assignments
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd)"
            r"(\s*[:=]\s*[\"']?)([^\s\"']{12,})"
        ),
        r"\1\2<redacted>",
    ),
)


def is_wiki_markdown_valid(wiki_markdown: str | None) -> bool:
    """Return true when wiki text has required headings with visible bodies."""
    if not isinstance(wiki_markdown, str) or not wiki_markdown.strip():
        return False
    return all(_section_has_visible_body(wiki_markdown, section) for section in _REQUIRED_SECTIONS)


def _section_has_visible_body(wiki_markdown: str, section: str) -> bool:
    body = _required_section_body(wiki_markdown, section)
    if body is None:
        return False
    return _has_visible_markdown_text(body)


def _required_section_body(wiki_markdown: str, section: str) -> str | None:
    section_heading_re = re.compile(
        rf"^(?P<marks>#{{1,6}})\s+{re.escape(section)}\s*(?:#+\s*)?$",
        re.MULTILINE,
    )
    match = section_heading_re.search(wiki_markdown)
    if match is None:
        return None

    level = len(match.group("marks"))
    body_start = match.end()
    body_end = len(wiki_markdown)
    for next_heading in _HEADING_RE.finditer(wiki_markdown, body_start):
        if len(next_heading.group("marks")) <= level:
            body_end = next_heading.start()
            break
    return wiki_markdown[body_start:body_end]


def _has_visible_markdown_text(body: str) -> bool:
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    visible_lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stripped = re.sub(r"^>\s?", "", stripped)
        stripped = re.sub(r"^\s{0,3}(?:[-*+]|\d+[.)])\s*", "", stripped)
        stripped = re.sub(r"^`{3,}.*$", "", stripped)
        stripped = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", stripped)
        stripped = re.sub(r"\[([^\]]*)\]\([^)]+\)", r"\1", stripped)
        stripped = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", r"\2\1", stripped)
        stripped = stripped.strip(" \t\r\n#`*_>-+|:;.,[](){}")
        if stripped:
            visible_lines.append(stripped)

    visible = " ".join(visible_lines)
    return any(char.isalnum() for char in visible)


def wiki_generation_skip_reason(
    session: Any,
    digest_markdown: str | None,
) -> str | None:
    """Return a skip reason when wiki synthesis should not run, else ``None``.

    The skip policy lives here (not only in lifecycle) so manual CLI/server/MCP
    refresh enforces the same skips as the background path: subagents, pipeline
    and cron sessions, and sessions below the digest-turn threshold are skipped.
    """
    if not digest_markdown or not digest_markdown.strip():
        return "no_digest"
    agent_depth = getattr(session, "agent_depth", 0) or 0
    if agent_depth > 0:
        return "subagent"
    source = getattr(session, "source", "") or ""
    if source in _EPHEMERAL_SOURCES:
        return f"ephemeral_source:{source}"
    if digest_turn_count(digest_markdown) < WIKI_MIN_DIGEST_TURNS:
        return "below_digest_threshold"
    return None


def redact_wiki_markdown(text: str) -> str:
    """Scrub secrets and the operator home path from synthesized wiki text.

    Applied once to the assembled page; the redacted body is the single source
    for both the DB column and the mirror file.
    """
    if not text:
        return text
    redacted = text
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    home = str(Path.home())
    if home and home != "/" and home in redacted:
        redacted = redacted.replace(home, "~")
    return redacted


def _extract_suggested_tags(synthesis: str) -> tuple[list[str], str]:
    """Split the leading ``<!-- suggested-tags: ... -->`` comment from the body.

    Returns ``(tags, body)`` where ``body`` is the synthesis with the comment
    removed and leading whitespace stripped.
    """
    match = _SUGGESTED_TAGS_RE.search(synthesis)
    tags: list[str] = []
    if match:
        raw = match.group("tags")
        tags = [tag.strip() for tag in raw.split(",") if tag.strip()]
    body = _SUGGESTED_TAGS_RE.sub("", synthesis, count=1).lstrip("\n").lstrip()
    return tags, body


def _session_date(session: Any) -> str:
    """Return the session's date (YYYY-MM-DD) from ``created_at``."""
    created_at = getattr(session, "created_at", None)
    if isinstance(created_at, str) and len(created_at) >= 10:
        return created_at[:10]
    return ""


def _format_meta_yaml(session: Any) -> str:
    """Build a compact YAML metadata block for the synthesis prompt context."""
    return _dump_yaml_mapping(
        {
            "project": getattr(session, "project_id", "") or "",
            "date": _session_date(session),
            "model": getattr(session, "model", "") or "",
            "source": getattr(session, "source", "") or "",
            "input_tokens": getattr(session, "usage_input_tokens", 0) or 0,
            "output_tokens": getattr(session, "usage_output_tokens", 0) or 0,
        }
    )


def _dump_yaml_mapping(values: dict[str, Any]) -> str:
    """Serialize prompt metadata/frontmatter with safe YAML quoting."""
    return yaml.safe_dump(
        values,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).strip()


def wiki_source_context_hash(
    *,
    session: Any,
    digest_markdown: str,
    prompt_path: str,
    rendered_prompt: str,
) -> str:
    """Hash the exact wiki source inputs used to decide regeneration."""
    return source_context_hash(
        {
            "external_id": getattr(session, "external_id", None),
            "digest_markdown": digest_markdown,
            "prompt_path": prompt_path,
            "rendered_prompt": rendered_prompt,
        }
    )


def _safe_wiki_filename_stem(session: Any) -> str:
    raw = str(getattr(session, "external_id", None) or getattr(session, "id", "") or "session")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.strip()).strip(".-_")
    return stem[:120] or "session"


def _build_wiki_frontmatter(session: Any, tags: list[str]) -> str:
    """Wrap the synthesized body with the daemon-owned frontmatter (the seam).

    gwiki parses and strips this on ingest, writing its own frontmatter.
    """
    external_id = getattr(session, "external_id", "") or ""
    short_id = external_id[:8] if external_id else (getattr(session, "id", "") or "")[:8]
    date = _session_date(session)
    title = f"Session: {short_id}" + (f" — {date}" if date else "")
    frontmatter = _dump_yaml_mapping(
        {
            "title": title,
            "type": "source",
            "tags": tags,
            "date": date,
            "model": getattr(session, "model", "") or "",
            "project": getattr(session, "project_id", "") or "",
            "session_id": getattr(session, "id", "") or "",
            "source": getattr(session, "source", "") or "",
        }
    )
    return f"---\n{frontmatter}\n---"


def assemble_wiki_page(session: Any, synthesis: str) -> tuple[list[str], str, str]:
    """Turn raw synthesis into a redacted, frontmatter-wrapped page.

    Returns ``(tags, body, file_content)`` where ``file_content`` is the
    redacted frontmatter + body written to both the DB column and the file.
    """
    tags, body = _extract_suggested_tags(synthesis)
    frontmatter = _build_wiki_frontmatter(session, tags)
    page = f"{frontmatter}\n\n{body.strip()}\n"
    return tags, body, redact_wiki_markdown(page)


def resolve_wiki_file_path(session: Any, session_wiki_config: Any) -> Path:
    """Resolve the mirror file path under gobby home.

    The configured ``wiki_file_path`` (default ``.gobby/session_wiki``) is
    relative to the gobby data root; a leading ``.gobby`` would duplicate gobby
    home, so it is stripped before joining. Absolute configs are honored as-is.
    """
    from gobby.cli.utils_config import get_gobby_home

    external_id = _safe_wiki_filename_stem(session)
    configured = getattr(session_wiki_config, "wiki_file_path", ".gobby/session_wiki")
    rel = Path(configured)
    if rel.is_absolute():
        base = rel
    else:
        parts = [part for part in rel.parts if part not in (".", ".gobby")]
        base = get_gobby_home().joinpath(*parts) if parts else get_gobby_home() / "session_wiki"
    return base / f"{external_id}.md"


def _render_wiki_prompt(
    *,
    session: Any,
    digest_markdown: str,
    prompt_path: str,
    db: Any | None,
    session_manager: Any,
) -> str | None:
    """Render the wiki synthesis prompt from the installed prompt record.

    Returns the rendered prompt, or ``None`` when the prompt record cannot be
    resolved (no usable DB / template missing) so the caller can skip cleanly.
    """
    resolved_db = _summary_context_db(db, session_manager)
    try:
        from gobby.prompts.loader import PromptLoader

        loader = PromptLoader(db=resolved_db)
        return loader.render(
            prompt_path,
            {"digest_markdown": digest_markdown, "meta": _format_meta_yaml(session)},
            strict=True,
        )
    except FileNotFoundError:
        logger.warning("Wiki synthesis prompt not found: %s", prompt_path)
        return None
    except Exception as exc:  # noqa: BLE001 — boundary: render is best-effort
        logger.warning("Failed to render wiki synthesis prompt %s: %s", prompt_path, exc)
        return None


async def generate_session_wiki(
    *,
    session: Any,
    digest_markdown: str | None,
    session_manager: Any,
    llm_service: Any | None,
    session_wiki_config: Any | None,
    db: Any | None = None,
    db_runner: Callable[..., Awaitable[Any]] | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """Synthesize and persist the session wiki page (best-effort tail step).

    Skips (without raising) when disabled, when synthesis is unwarranted (skip
    policy / threshold), when nothing changed since the last revision, or when
    the LLM produces no usable output. Never falls back to a transcript-like
    artifact: the page is digest-backed synthesis only.
    """
    if session_wiki_config is None or not getattr(session_wiki_config, "enabled", False):
        return {"generated": False, "skipped": "disabled"}
    if llm_service is None:
        return {"generated": False, "skipped": "no_llm_service"}

    skip_reason = wiki_generation_skip_reason(session, digest_markdown)
    if skip_reason is not None:
        return {"generated": False, "skipped": skip_reason}

    assert isinstance(digest_markdown, str)  # narrowed by wiki_generation_skip_reason
    session_id = getattr(session, "id", None)
    if not session_id:
        return {"generated": False, "skipped": "no_session_id"}

    prompt_path = getattr(session_wiki_config, "prompt_path", WIKI_PROMPT_PATH)
    current_turns = digest_turn_count(digest_markdown)
    prompt = _render_wiki_prompt(
        session=session,
        digest_markdown=digest_markdown,
        prompt_path=prompt_path,
        db=db,
        session_manager=session_manager,
    )
    if not prompt:
        return {"generated": False, "skipped": "prompt_unavailable"}

    current_hash = wiki_source_context_hash(
        session=session,
        digest_markdown=digest_markdown,
        prompt_path=prompt_path,
        rendered_prompt=prompt,
    )

    previous_hash = getattr(session, "wiki_source_context_hash", None)
    previous_turns = coerce_digest_turn_count(getattr(session, "wiki_digest_turn_count", None))
    existing_valid = is_wiki_markdown_valid(getattr(session, "wiki_markdown", None))
    if (
        isinstance(previous_hash, str)
        and previous_hash == current_hash
        and previous_turns is not None
        and existing_valid
    ):
        return {"generated": False, "skipped": "noop", "reason": "source_context_hash_match"}

    try:
        synthesis = await llm_service.call_feature(
            session_wiki_config,
            prompt,
            system_prompt=(
                "You are maintaining a knowledge wiki. Synthesize a concise, "
                "cross-linked source page from the session digest."
            ),
            caller="sessions.wiki",
            cwd=project_path,
        )
    except Exception as exc:  # noqa: BLE001 — boundary: LLM call is best-effort
        logger.warning("Wiki synthesis LLM call failed for %s: %s", session_id, exc)
        await _record_wiki_synthesis_failure(
            session_id=session_id,
            session_manager=session_manager,
            db_runner=db_runner,
            reason="llm_error",
            error=str(exc),
        )
        return {"generated": False, "skipped": "llm_error", "error": str(exc)}

    if not synthesis or not synthesis.strip():
        await _record_wiki_synthesis_failure(
            session_id=session_id,
            session_manager=session_manager,
            db_runner=db_runner,
            reason="empty_synthesis",
        )
        return {"generated": False, "skipped": "empty_synthesis"}

    tags, _body, file_content = assemble_wiki_page(session, synthesis)
    if not is_wiki_markdown_valid(file_content):
        await _record_wiki_synthesis_failure(
            session_id=session_id,
            session_manager=session_manager,
            db_runner=db_runner,
            reason="invalid_synthesis",
        )
        return {"generated": False, "skipped": "invalid_synthesis"}

    wiki_path = resolve_wiki_file_path(session, session_wiki_config)
    metadata = {"reason": "full", "tags": tags}

    persisted = await _persist_wiki_state(
        session_id=session_id,
        session_manager=session_manager,
        db_runner=db_runner,
        wiki_markdown=file_content,
        source_hash=current_hash,
        digest_turns=current_turns,
        wiki_path=str(wiki_path),
        metadata=metadata,
    )

    _write_wiki_file(wiki_path, file_content)

    logger.info(
        "Session wiki generated for %s (%d chars, %d tags)",
        session_id,
        len(file_content),
        len(tags),
    )
    return {
        "generated": True,
        "wiki_path": str(wiki_path),
        "wiki_length": len(file_content),
        "tags": tags,
        "source_context_hash": current_hash,
        "digest_turn_count": current_turns,
        "persisted": persisted is not None,
    }


async def _record_wiki_synthesis_failure(
    *,
    session_id: str,
    session_manager: Any,
    db_runner: Callable[..., Awaitable[Any]] | None,
    reason: str,
    error: str | None = None,
) -> Any | None:
    """Record durable wiki synthesis failure state when storage supports it."""
    record_failure = getattr(session_manager, "record_wiki_synthesis_failure", None)
    if not callable(record_failure):
        return None

    from gobby.sessions.summarize import _run_db

    try:
        return await _run_db(db_runner, record_failure, session_id, reason, error=error)
    except Exception as exc:  # noqa: BLE001 — boundary: failure tracking is best-effort
        logger.warning(
            "Failed to record wiki synthesis failure for %s: %s",
            session_id,
            exc,
        )
        return None


async def _persist_wiki_state(
    *,
    session_id: str,
    session_manager: Any,
    db_runner: Callable[..., Awaitable[Any]] | None,
    wiki_markdown: str,
    source_hash: str,
    digest_turns: int,
    wiki_path: str,
    metadata: dict[str, Any],
) -> Any | None:
    """Persist wiki state via the storage mixin, bridged through the executor."""
    persist_wiki_state = getattr(session_manager, "persist_wiki_state", None)
    if not callable(persist_wiki_state):
        return None

    from gobby.sessions.summarize import _run_db

    return await _run_db(
        db_runner,
        persist_wiki_state,
        session_id,
        wiki_markdown=wiki_markdown,
        generation_mode=WIKI_GENERATION_MODE,
        source_context_hash=source_hash,
        digest_turn_count=digest_turns,
        metadata_json=metadata,
        wiki_path=wiki_path,
    )


def _write_wiki_file(wiki_path: Path, content: str) -> None:
    """Write the redacted mirror file, creating parent dirs. Best-effort."""
    try:
        wiki_path.parent.mkdir(parents=True, exist_ok=True)
        wiki_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to write wiki mirror file %s: %s", wiki_path, exc)


# Candidate sessions are paged from the coarse SQL prefilter in chunks of this
# size; eligibility is then resolved per-row in Python.
_WIKI_BACKFILL_PAGE_SIZE = 200

#: ``generate_session_wiki`` skip reasons that are NOT failures for an eligible
#: (missing) wiki. Deliberately narrow: the backfill pre-checks skip policy and
#: wiki validity, so the only benign non-generated outcome is an unchanged
#: source-context hash (``noop``). Everything else (``llm_error``,
#: ``empty_synthesis``, ``invalid_synthesis``, ``prompt_unavailable``,
#: ``disabled``, ``no_llm_service``, ...) is counted as a failure.
_BENIGN_WIKI_SKIPS = frozenset({"noop"})


@dataclass
class WikiBackfillFailure:
    """A single session that could not be (re)synthesized during backfill."""

    session_id: str
    reason: str | None
    error: str | None = None


@dataclass
class WikiBackfillResult:
    """Outcome of a session-wiki backfill pass."""

    scanned: int = 0  # coarse candidates examined
    eligible: int = 0  # passed skip policy and lacked a valid wiki
    synthesized: int = 0  # wiki generated this run
    skipped: int = 0  # ineligible (skip policy / already-valid / benign noop)
    failed: int = 0  # synthesis attempted but did not produce a valid wiki
    attempts: int = 0  # synthesis calls made (0 in dry-run)
    failures: list[WikiBackfillFailure] = field(default_factory=list)


async def backfill_session_wikis(
    *,
    session_manager: Any,
    llm_service: Any | None,
    session_summary_config: Any | None,
    session_wiki_config: Any | None,
    db: Any | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> WikiBackfillResult:
    """Re-synthesize missing/invalid session wikis from stored digests.

    One-time-but-reusable backfill. Pages through eligible sessions (top-level,
    non-ephemeral, digest with at least ``WIKI_MIN_DIGEST_TURNS`` turns, and no
    valid wiki) and re-runs the shared summary+wiki flow from each session's
    stored digest — no transcript needed. Idempotent: a session whose wiki is
    already valid is skipped, so re-running only fills gaps and retries prior
    failures.

    ``limit`` bounds synthesis *attempts* (or, in ``dry_run``, the number of
    eligible sessions counted), not rows scanned, so a small limit still does
    real work when early candidates are already valid. Runs sequentially to
    avoid rate-limit handling for a large one-time batch; one failing session
    never aborts the batch.
    """
    from gobby.sessions.summarize import generate_session_summaries

    result = WikiBackfillResult()
    after_id: str | None = None

    while True:
        candidates = session_manager.get_wiki_backfill_candidates(
            limit=_WIKI_BACKFILL_PAGE_SIZE, after_id=after_id
        )
        if not candidates:
            break

        for session in candidates:
            after_id = session.id
            result.scanned += 1

            digest = getattr(session, "digest_markdown", None)
            if wiki_generation_skip_reason(session, digest) is not None:
                result.skipped += 1
                continue
            if is_wiki_markdown_valid(getattr(session, "wiki_markdown", None)):
                result.skipped += 1
                continue

            result.eligible += 1
            if dry_run:
                if limit is not None and result.eligible >= limit:
                    return result
                continue

            result.attempts += 1
            try:
                res = await generate_session_summaries(
                    session_id=session.id,
                    session_manager=session_manager,
                    llm_service=llm_service,
                    session_summary_config=session_summary_config,
                    db=db,
                    set_handoff_ready=False,
                    session_wiki_config=session_wiki_config,
                )
            except Exception as exc:  # noqa: BLE001 — one bad session must not abort the batch
                result.failed += 1
                result.failures.append(WikiBackfillFailure(session.id, "exception", str(exc)))
                if progress:
                    progress(f"  FAILED {session.id[:12]}: {exc}")
            else:
                _record_backfill_outcome(result, session.id, res, progress)

            if limit is not None and result.attempts >= limit:
                return result

    return result


def _record_backfill_outcome(
    result: WikiBackfillResult,
    session_id: str,
    res: dict[str, Any],
    progress: Callable[[str], None] | None,
) -> None:
    """Classify one ``generate_session_summaries`` result into the tally.

    A failed summary call, or a non-generated wiki whose skip reason is not in
    ``_BENIGN_WIKI_SKIPS``, counts as a failure for an eligible (missing) wiki.
    """
    if not res.get("success"):
        result.failed += 1
        result.failures.append(WikiBackfillFailure(session_id, "summary_error", res.get("error")))
        if progress:
            progress(f"  FAILED {session_id[:12]}: summary {res.get('error')}")
        return

    wiki = res.get("wiki") or {}
    if wiki.get("generated"):
        result.synthesized += 1
        if progress:
            progress(f"  synthesized {session_id[:12]} ({wiki.get('wiki_length', 0)} chars)")
        return

    skip_reason = wiki.get("skipped")
    if skip_reason in _BENIGN_WIKI_SKIPS:
        result.skipped += 1
        return

    result.failed += 1
    result.failures.append(WikiBackfillFailure(session_id, skip_reason, wiki.get("error")))
    if progress:
        progress(f"  FAILED {session_id[:12]}: wiki {skip_reason}")
