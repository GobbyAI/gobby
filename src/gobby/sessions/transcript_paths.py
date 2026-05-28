"""Transcript path discovery helpers."""

from __future__ import annotations

from pathlib import Path


def _find_transcript_on_disk(
    source: str,
    external_id: str,
    max_days: int = 90,
) -> str | None:
    """Try to find a transcript file on disk by CLI source and external_id."""
    if not external_id:
        return None

    if source == "claude":
        projects_dir = Path.home() / ".claude" / "projects"
        if projects_dir.exists():
            for proj_dir in projects_dir.iterdir():
                if not proj_dir.is_dir():
                    continue
                candidate = proj_dir / f"{external_id}.jsonl"
                if candidate.is_file():
                    return str(candidate)

    elif source == "codex":
        sessions_dir = Path.home() / ".codex" / "sessions"
        if sessions_dir.exists():
            inspected_days = 0
            for year_dir in sorted(sessions_dir.iterdir(), reverse=True):
                if not year_dir.is_dir():
                    continue
                for month_dir in sorted(year_dir.iterdir(), reverse=True):
                    if not month_dir.is_dir():
                        continue
                    for day_dir in sorted(month_dir.iterdir(), reverse=True):
                        if not day_dir.is_dir():
                            continue
                        if inspected_days >= max_days:
                            return None
                        inspected_days += 1
                        matches = list(day_dir.glob(f"*{external_id}*"))
                        if matches:
                            return str(matches[0])

    elif source == "gemini":
        gemini_tmp = Path.home() / ".gemini" / "tmp"
        prefix = external_id[:8] if external_id else ""
        if gemini_tmp.exists() and prefix:
            for proj_dir in gemini_tmp.iterdir():
                chats_dir = proj_dir / "chats"
                if not chats_dir.is_dir():
                    continue
                matches = sorted(chats_dir.glob(f"session-*-{prefix}.json"), reverse=True)
                if matches:
                    return str(matches[0])
    elif source == "qwen":
        qwen_projects = Path.home() / ".qwen" / "projects"
        if qwen_projects.exists():
            for proj_dir in qwen_projects.iterdir():
                chats_dir = proj_dir / "chats"
                if not chats_dir.is_dir():
                    continue
                matches = sorted(chats_dir.glob(f"*{external_id}*.jsonl"), reverse=True)
                if matches:
                    return str(matches[0])
    elif source == "grok":
        grok_sessions = Path.home() / ".grok" / "sessions"
        if grok_sessions.exists():
            matches = sorted(
                grok_sessions.glob(f"*/{external_id}/updates.jsonl"),
                reverse=True,
            )
            if matches:
                return str(matches[0])
    elif source == "droid":
        droid_sessions = Path.home() / ".factory" / "sessions"
        if droid_sessions.exists():
            for proj_dir in droid_sessions.iterdir():
                if not proj_dir.is_dir():
                    continue
                candidate = proj_dir / f"{external_id}.jsonl"
                if candidate.is_file():
                    return str(candidate)

    return None


def _is_json_session_file(path: str) -> bool:
    """Check if a transcript file is a native JSON session file."""
    return path.endswith(".json")
