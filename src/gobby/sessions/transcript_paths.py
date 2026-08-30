"""Transcript path discovery helpers."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from glob import escape as glob_escape
from pathlib import Path
from time import time
from typing import Literal
from urllib.parse import quote

from gobby.sessions.machine_scope import is_local_machine_owner

_SECONDS_PER_DAY = 24 * 60 * 60
MISSING_TRANSCRIPT_PATH = "missing_transcript"

TranscriptCallerContext = Literal["hook", "recovery"]


class TranscriptPathStatus(StrEnum):
    """Classification of a hook-reported or stored transcript path."""

    USABLE = "usable"
    PENDING = "pending"
    INVALID = "invalid"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class _PathDetectRule:
    parts: tuple[str, ...] = ()
    name_prefix: str | None = None
    name_suffix: str | None = None

    def matches(self, parts: tuple[str, ...], name: str, lowered: str) -> bool:
        if self.name_prefix is not None or self.name_suffix is not None:
            prefix_ok = self.name_prefix is None or name.startswith(self.name_prefix)
            suffix_ok = self.name_suffix is None or lowered.endswith(self.name_suffix)
            if prefix_ok and suffix_ok:
                return not self.parts or all(part in parts for part in self.parts)
            return False
        return bool(self.parts) and all(part in parts for part in self.parts)


@dataclass(frozen=True, slots=True)
class TranscriptProviderSpec:
    """Per-provider transcript layout: detection, hook candidates, recovery."""

    source: str
    detect_rules: tuple[_PathDetectRule, ...]
    hook_relpath: str | None = None
    hook_requires_cwd: bool = False
    hook_search: Callable[..., list[Path]] | None = None
    recover: Callable[[Path, str, str, int], str | None] | None = None


def classify_transcript_path(path: object) -> tuple[TranscriptPathStatus, str | None]:
    """Classify a hook-reported path as usable, pending, invalid, or absent."""
    if path is None:
        return TranscriptPathStatus.ABSENT, None
    if not isinstance(path, str):
        return TranscriptPathStatus.INVALID, None
    stripped = path.strip()
    if not stripped:
        return TranscriptPathStatus.ABSENT, None
    if "\0" in stripped:
        return TranscriptPathStatus.INVALID, None
    expanded = str(Path(stripped).expanduser())
    try:
        candidate = Path(expanded)
        if candidate.is_file():
            if os.access(candidate, os.R_OK):
                return TranscriptPathStatus.USABLE, expanded
            return TranscriptPathStatus.INVALID, expanded
        if candidate.exists():
            return TranscriptPathStatus.INVALID, expanded
        return TranscriptPathStatus.PENDING, expanded
    except OSError:
        return TranscriptPathStatus.INVALID, None


def usable_transcript_path(path: object) -> str | None:
    """Return an expanded path only when it exists and is readable."""
    status, expanded = classify_transcript_path(path)
    if status is TranscriptPathStatus.USABLE:
        return expanded
    return None


def detect_source_from_path(path: str | None) -> str | None:
    """Infer transcript source from the per-provider path-shape table."""
    if not path:
        return None
    normalized = str(Path(path).expanduser())
    lowered = normalized.lower()
    parts = Path(normalized).parts
    name = Path(normalized).name
    for spec in PROVIDER_TRANSCRIPT_SPECS:
        for rule in spec.detect_rules:
            if rule.matches(parts, name, lowered):
                return spec.source
    return None


def find_transcript_on_disk(
    source: str,
    external_id: str,
    source_max_days: int = 90,
    *,
    owner_machine_id: str | None,
    local_machine_id: str | None,
    max_days: int | None = None,
    caller_context: TranscriptCallerContext = "recovery",
    cwd: str | None = None,
    session_id: str | None = None,
) -> str | None:
    """Find a transcript file on disk by CLI source and external_id.

    ``caller_context="hook"`` checks only bounded direct candidates from the
    per-provider table. ``caller_context="recovery"`` may traverse provider
    layouts. Async callers must run this in a thread.
    """
    if not is_local_machine_owner(owner_machine_id, local_machine_id):
        return None
    if not external_id:
        return None
    if "/" in external_id or "\\" in external_id:
        return None
    if max_days is not None:
        source_max_days = max_days
    _validate_max_days(source_max_days)
    spec = _SPECS_BY_SOURCE.get(source)
    if spec is None:
        return None
    home = Path.home()
    resolved_session_id = session_id or external_id
    if caller_context == "hook":
        return _first_readable(
            _hook_candidates(
                spec,
                home=home,
                external_id=external_id,
                session_id=resolved_session_id,
                cwd=cwd,
            )
        )
    if spec.recover is None:
        return None
    return spec.recover(home, external_id, glob_escape(external_id), source_max_days)


def _first_readable(paths: list[Path]) -> str | None:
    for path in paths:
        if _is_readable_file(path):
            return str(path)
    return None


def _hook_candidates(
    spec: TranscriptProviderSpec,
    *,
    home: Path,
    external_id: str,
    session_id: str,
    cwd: str | None,
) -> list[Path]:
    if spec.hook_search is not None:
        return spec.hook_search(
            home=home,
            cwd=cwd,
            external_id=external_id,
            session_id=session_id,
        )
    if spec.hook_relpath is None:
        return []
    encoded_cwd = ""
    if spec.hook_requires_cwd:
        if not cwd:
            return []
        encoded_cwd = quote(str(cwd), safe="")
    relative = spec.hook_relpath.format(
        external_id=external_id,
        session_id=session_id,
        encoded_cwd=encoded_cwd,
    )
    return [home.joinpath(*Path(relative).parts)]


def _qwen_tmp_hook_candidates(
    *,
    home: Path,
    cwd: str | None,
    external_id: str,
    session_id: str,
) -> list[Path]:
    if not cwd:
        return []
    project_hash = hashlib.sha256(cwd.encode()).hexdigest()
    chats_dir = home / ".qwen" / "tmp" / project_hash / "chats"
    if not _safe_is_dir(chats_dir):
        return []
    prefix = (session_id or external_id)[:8]
    if not prefix:
        return []
    return [
        path
        for path in _safe_glob(chats_dir, f"session-*-{prefix}.json", reverse=True)
        if _is_readable_file(path)
    ]


def _recover_claude(home: Path, external_id: str, escaped: str, max_days: int) -> str | None:
    del escaped
    projects_dir = home / ".claude" / "projects"
    if not _safe_exists(projects_dir):
        return None
    for proj_dir in _safe_iterdir(projects_dir):
        if not _safe_is_dir(proj_dir):
            continue
        candidate = proj_dir / f"{external_id}.jsonl"
        if _is_recent_file(candidate, max_days):
            return str(candidate)
    return None


def _recover_codex(home: Path, external_id: str, escaped: str, max_days: int) -> str | None:
    sessions_dir = home / ".codex" / "sessions"
    if not _safe_exists(sessions_dir):
        return None
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
                    _safe_glob(day_dir, f"rollout-*{escaped}.jsonl"),
                    max_days,
                )
                if match:
                    return str(match)
    return None


def _recover_qwen(home: Path, external_id: str, escaped: str, max_days: int) -> str | None:
    del external_id
    qwen_projects = home / ".qwen" / "projects"
    if not _safe_exists(qwen_projects):
        return None
    for proj_dir in _safe_iterdir(qwen_projects):
        chats_dir = proj_dir / "chats"
        if not _safe_is_dir(chats_dir):
            continue
        match = _first_recent_file(
            _safe_glob(chats_dir, f"*{escaped}*.jsonl", reverse=True),
            max_days,
        )
        if match:
            return str(match)
    return None


def _recover_grok(home: Path, external_id: str, escaped: str, max_days: int) -> str | None:
    del external_id
    grok_sessions = home / ".grok" / "sessions"
    if not _safe_exists(grok_sessions):
        return None
    match = _first_recent_file(
        _safe_glob(grok_sessions, f"*/{escaped}/updates.jsonl", reverse=True),
        max_days,
    )
    if match:
        return str(match)
    return None


def _recover_droid(home: Path, external_id: str, escaped: str, max_days: int) -> str | None:
    del escaped
    droid_sessions = home / ".factory" / "sessions"
    if not _safe_exists(droid_sessions):
        return None
    for proj_dir in _safe_iterdir(droid_sessions):
        if not _safe_is_dir(proj_dir):
            continue
        candidate = proj_dir / f"{external_id}.jsonl"
        if _is_recent_file(candidate, max_days):
            return str(candidate)
    return None


def _recover_agy(home: Path, external_id: str, escaped: str, max_days: int) -> str | None:
    del escaped
    candidate = (
        home
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / external_id
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )
    if _is_recent_file(candidate, max_days):
        return str(candidate)
    return None


PROVIDER_TRANSCRIPT_SPECS: tuple[TranscriptProviderSpec, ...] = (
    TranscriptProviderSpec(
        source="claude",
        detect_rules=(_PathDetectRule(parts=(".claude", "projects")),),
        recover=_recover_claude,
    ),
    TranscriptProviderSpec(
        source="codex",
        detect_rules=(
            _PathDetectRule(parts=(".codex", "sessions")),
            _PathDetectRule(name_prefix="rollout-", name_suffix=".jsonl"),
        ),
        recover=_recover_codex,
    ),
    TranscriptProviderSpec(
        source="qwen",
        detect_rules=(_PathDetectRule(parts=(".qwen",)),),
        hook_search=_qwen_tmp_hook_candidates,
        recover=_recover_qwen,
    ),
    TranscriptProviderSpec(
        source="grok",
        detect_rules=(_PathDetectRule(parts=(".grok", "sessions")),),
        hook_relpath=".grok/sessions/{encoded_cwd}/{session_id}/updates.jsonl",
        hook_requires_cwd=True,
        recover=_recover_grok,
    ),
    TranscriptProviderSpec(
        source="droid",
        detect_rules=(_PathDetectRule(parts=(".factory", "sessions")),),
        recover=_recover_droid,
    ),
    TranscriptProviderSpec(
        source="agy",
        detect_rules=(_PathDetectRule(parts=(".gemini", "antigravity-cli", "brain")),),
        hook_relpath=(
            ".gemini/antigravity-cli/brain/{external_id}"
            "/.system_generated/logs/transcript_full.jsonl"
        ),
        recover=_recover_agy,
    ),
)
_SPECS_BY_SOURCE = {spec.source: spec for spec in PROVIDER_TRANSCRIPT_SPECS}


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


def _is_readable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.R_OK)
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
    _validate_max_days(max_days)
    try:
        return path.is_file() and path.stat().st_mtime >= time() - (max_days * _SECONDS_PER_DAY)
    except OSError:
        return False


def _validate_max_days(max_days: int) -> None:
    if max_days <= 0:
        raise ValueError("max_days must be positive")
