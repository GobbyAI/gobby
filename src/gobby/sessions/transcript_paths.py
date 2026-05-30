"""Transcript path discovery helpers."""

from __future__ import annotations

from pathlib import Path
from time import time

_SECONDS_PER_DAY = 24 * 60 * 60


def _find_transcript_on_disk(
    source: str,
    external_id: str,
    max_days: int = 90,
) -> str | None:
    """Try to find a transcript file on disk by CLI source and external_id.

    This performs blocking filesystem traversal; async callers must run it in a thread.
    """
    if not external_id:
        return None

    if source == "claude":
        projects_dir = Path.home() / ".claude" / "projects"
        if _safe_exists(projects_dir):
            for proj_dir in _safe_iterdir(projects_dir):
                if not _safe_is_dir(proj_dir):
                    continue
                candidate = proj_dir / f"{external_id}.jsonl"
                if _is_recent_file(candidate, max_days):
                    return str(candidate)

    elif source == "codex":
        sessions_dir = Path.home() / ".codex" / "sessions"
        if _safe_exists(sessions_dir):
            inspected_days = 0
            for year_dir in _safe_sorted_iterdir(sessions_dir, reverse=True):
                if not _safe_is_dir(year_dir):
                    continue
                for month_dir in _safe_sorted_iterdir(year_dir, reverse=True):
                    if not _safe_is_dir(month_dir):
                        continue
                    for day_dir in _safe_sorted_iterdir(month_dir, reverse=True):
                        if not _safe_is_dir(day_dir):
                            continue
                        inspected_days += 1
                        if inspected_days > max_days:
                            return None
                        match = _first_recent_file(
                            _safe_glob(day_dir, f"*{external_id}*"),
                            max_days,
                        )
                        if match:
                            return str(match)

    elif source == "gemini":
        gemini_tmp = Path.home() / ".gemini" / "tmp"
        prefix = external_id[:8] if external_id else ""
        if _safe_exists(gemini_tmp) and prefix:
            for proj_dir in _safe_iterdir(gemini_tmp):
                chats_dir = proj_dir / "chats"
                if not _safe_is_dir(chats_dir):
                    continue
                match = _first_recent_file(
                    _safe_glob(chats_dir, f"session-*-{prefix}.json", reverse=True),
                    max_days,
                )
                if match:
                    return str(match)
    elif source == "qwen":
        qwen_projects = Path.home() / ".qwen" / "projects"
        if _safe_exists(qwen_projects):
            for proj_dir in _safe_iterdir(qwen_projects):
                chats_dir = proj_dir / "chats"
                if not _safe_is_dir(chats_dir):
                    continue
                match = _first_recent_file(
                    _safe_glob(chats_dir, f"*{external_id}*.jsonl", reverse=True),
                    max_days,
                )
                if match:
                    return str(match)
    elif source == "grok":
        grok_sessions = Path.home() / ".grok" / "sessions"
        if _safe_exists(grok_sessions):
            match = _first_recent_file(
                _safe_glob(grok_sessions, f"*/{external_id}/updates.jsonl", reverse=True),
                max_days,
            )
            if match:
                return str(match)
    elif source == "droid":
        droid_sessions = Path.home() / ".factory" / "sessions"
        if _safe_exists(droid_sessions):
            for proj_dir in _safe_iterdir(droid_sessions):
                if not _safe_is_dir(proj_dir):
                    continue
                candidate = proj_dir / f"{external_id}.jsonl"
                if _is_recent_file(candidate, max_days):
                    return str(candidate)

    return None


def _is_json_session_file(path: str) -> bool:
    """Check if a transcript file is a native JSON session file."""
    return path.endswith(".json")


def _safe_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _safe_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def _safe_sorted_iterdir(path: Path, *, reverse: bool = False) -> list[Path]:
    return sorted(_safe_iterdir(path), reverse=reverse)


def _safe_glob(path: Path, pattern: str, *, reverse: bool = False) -> list[Path]:
    try:
        return sorted(path.glob(pattern), reverse=reverse)
    except OSError:
        return []


def _first_recent_file(paths: list[Path], max_days: int) -> Path | None:
    for path in paths:
        if _is_recent_file(path, max_days):
            return path
    return None


def _is_recent_file(path: Path, max_days: int) -> bool:
    if max_days <= 0:
        return False
    try:
        return path.is_file() and path.stat().st_mtime >= time() - (max_days * _SECONDS_PER_DAY)
    except OSError:
        return False
