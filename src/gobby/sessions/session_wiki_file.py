"""Write the durable session-wiki page (the daemon-owned flat file).

The session wiki page IS the session summary. ``generate_session_summaries``
writes the redacted ``summary_markdown`` to a single machine-global flat file
(``~/.gobby/session_wiki/{external_id}.md``) that gwiki ingests into its
``sessions`` topic vault. This module owns the frontmatter shape, redaction,
path resolution, and file write — all DB-free and LLM-free (the summary is
already generated upstream by the summary flow).

Security: a summary can lift secrets from the transcript verbatim, so the
assembled page is redacted ONCE before it touches disk; the redacted body is the
only thing written.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from gobby.sessions.summary_validity import is_summary_markdown_valid

logger = logging.getLogger(__name__)

#: Ephemeral session sources that never warrant a durable wiki page. Mirrors the
#: summary skip policy so the wiki page never appears where the summary wouldn't.
_EPHEMERAL_SOURCES = ("pipeline", "cron")

# Redaction patterns applied once to the assembled page before it is written.
# Order matters: specific secrets first, then the home-directory path.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # OpenAI / Anthropic style keys: sk-..., sk-proj-..., sk-ant-...
    (re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{15,}"), "sk-<redacted>"),
    # GitHub tokens: ghp_, gho_, ghu_, ghs_, ghr_, github_pat_
    (
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{22,})"),
        "gh<redacted-token>",
    ),
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


def redact_session_markdown(text: str) -> str:
    """Scrub secrets and the operator home path from session-wiki text.

    Applied once to the assembled page; the redacted body is what is written to
    disk and ingested by gwiki.
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


def _session_date(session: Any) -> str:
    """Return the session's date (YYYY-MM-DD) from ``created_at``."""
    created_at = getattr(session, "created_at", None)
    if isinstance(created_at, str) and len(created_at) >= 10:
        return created_at[:10]
    return ""


def _dump_yaml_mapping(values: dict[str, Any]) -> str:
    """Serialize frontmatter with safe YAML quoting."""
    return yaml.safe_dump(
        values,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).strip()


def _safe_filename_stem(session: Any) -> str:
    raw = str(getattr(session, "external_id", None) or getattr(session, "id", "") or "session")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.strip()).strip(".-_")
    return stem[:120] or "session"


def _build_frontmatter(session: Any, tags: list[str]) -> str:
    """Build the daemon-owned frontmatter block (the gwiki ingest seam).

    gwiki parses and strips this on ingest, writing its own frontmatter. Values
    are quoted via ``yaml.safe_dump`` and each stays on one flat ``key: value``
    line so gwiki's flat-frontmatter parser reads them. gwiki only needs
    ``source``/``model``/``project``/``tags``; the extra keys are harmless.
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


def resolve_session_wiki_path(session: Any) -> Path:
    """Resolve the flat session-wiki file path under gobby home.

    Always ``<gobby_home>/session_wiki/{external_id}.md`` — one machine-global
    directory the daemon owns and gwiki ingests.
    """
    from gobby.cli.utils_config import get_gobby_home

    return get_gobby_home() / "session_wiki" / f"{_safe_filename_stem(session)}.md"


def session_wiki_path_exists(session: Any) -> bool:
    """Return true when the session's flat wiki file is present on disk."""
    try:
        return resolve_session_wiki_path(session).is_file()
    except OSError as exc:
        logger.warning("Failed to stat session wiki file: %s", exc)
        return False


def session_wiki_path_is_fresh(session: Any) -> bool:
    """Return true when the flat wiki file exists and is not older than the summary."""
    try:
        stat = resolve_session_wiki_path(session).stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Failed to stat session wiki file: %s", exc)
        return False

    summary_generated_at = _parse_datetime(getattr(session, "summary_generated_at", None))
    if summary_generated_at is None:
        return True
    return stat.st_mtime >= summary_generated_at.timestamp()


def _write_file(path: Path, content: str) -> bool:
    """Write the redacted page, creating parent dirs. Best-effort."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("Failed to write session wiki file %s: %s", path, exc)
        return False


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _skip_reason(session: Any) -> str | None:
    """Return a skip reason when no durable wiki page should be written.

    Subagents and ephemeral (pipeline/cron) sessions are skipped, matching the
    summary skip policy so the wiki page never appears where the summary
    wouldn't.
    """
    if (getattr(session, "agent_depth", 0) or 0) > 0:
        return "subagent"
    source = getattr(session, "source", "") or ""
    if source in _EPHEMERAL_SOURCES:
        return f"ephemeral_source:{source}"
    return None


def write_session_wiki_page(session: Any, summary_markdown: str | None) -> dict[str, Any]:
    """Write the redacted session summary to the flat session-wiki file.

    The session wiki page is the session summary: this wraps ``summary_markdown``
    with daemon-owned frontmatter, redacts once, and writes the flat file gwiki
    ingests. Best-effort and side-effect-only — returns a status dict and never
    raises. Skips invalid summaries, subagents, and ephemeral sources.
    """
    if not is_summary_markdown_valid(summary_markdown):
        return {"written": False, "skipped": "invalid_summary"}
    skip = _skip_reason(session)
    if skip is not None:
        return {"written": False, "skipped": skip}

    assert isinstance(summary_markdown, str)  # narrowed by is_summary_markdown_valid
    tags: list[str] = []
    frontmatter = _build_frontmatter(session, tags)
    page = f"{frontmatter}\n\n{summary_markdown.strip()}\n"
    redacted = redact_session_markdown(page)
    path = resolve_session_wiki_path(session)
    if not _write_file(path, redacted):
        return {"written": False, "skipped": "write_failed", "path": str(path)}
    logger.info(
        "Session wiki page written for %s (%d chars)",
        getattr(session, "id", "?"),
        len(redacted),
    )
    return {"written": True, "path": str(path), "length": len(redacted)}
