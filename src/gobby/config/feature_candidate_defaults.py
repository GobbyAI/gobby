"""Guards for persisted feature candidate defaults."""

from __future__ import annotations

import json
import logging
from typing import Any

import psycopg

from gobby.config.feature_base import FeatureProfile

logger = logging.getLogger(__name__)

_OLD_CLAUDE_ONLY_CANDIDATES: dict[FeatureProfile, list[str]] = {
    FeatureProfile.LOW: ["claude/haiku"],
    FeatureProfile.MID: ["claude/sonnet"],
    FeatureProfile.HIGH: ["claude/opus"],
}

_FEATURE_CANDIDATE_PROFILES: dict[str, FeatureProfile] = {
    "session_summary.candidates": FeatureProfile.LOW,
    "digest.candidates": FeatureProfile.LOW,
    "memory_recall.candidates": FeatureProfile.LOW,
    "memory.kg.candidates": FeatureProfile.LOW,
    "memory.dream.candidates": FeatureProfile.MID,
    "tool_summarizer.candidates": FeatureProfile.LOW,
    "recommend_tools.candidates": FeatureProfile.MID,
    "import_mcp_server.candidates": FeatureProfile.LOW,
    "skill_description.candidates": FeatureProfile.LOW,
    "merge_resolution.candidates": FeatureProfile.MID,
    "gobby-tasks.expansion.candidates": FeatureProfile.HIGH,
    "gobby-tasks.validation.candidates": FeatureProfile.MID,
    "chat.candidates": FeatureProfile.HIGH,
    "code_index.summary_candidates": FeatureProfile.LOW,
}


def reject_stale_default_feature_candidate_rows(config_store: Any | None) -> None:
    """Fail fast when old defaults-seeded Claude-only candidates are still persisted."""
    db = getattr(config_store, "db", None)
    fetchall = getattr(db, "fetchall", None)
    if not callable(fetchall):
        return

    try:
        rows = fetchall(
            "SELECT key, value FROM config_store WHERE source = %s",
            ("defaults",),
        )
    except psycopg.Error as exc:
        logger.debug("Failed to inspect defaults-seeded feature candidate rows: %s", exc)
        return

    stale_keys = sorted(key for row in rows if (key := _get_stale_candidate_key(row)) is not None)
    if not stale_keys:
        return

    joined = ", ".join(stale_keys)
    raise ValueError(
        "Stale defaults-seeded Claude-only feature candidate rows found in "
        f"config_store: {joined}. Gobby 0.5.0 does not rewrite old defaults at "
        "startup; delete these defaults or reseed config defaults so Codex-first "
        "profile candidates apply."
    )


def _get_stale_candidate_key(row: Any) -> str | None:
    key = _row_value(row, "key", 0)
    if not isinstance(key, str) or key not in _FEATURE_CANDIDATE_PROFILES:
        return None
    value = _decoded_value(_row_value(row, "value", 1))
    expected = _OLD_CLAUDE_ONLY_CANDIDATES[_FEATURE_CANDIDATE_PROFILES[key]]
    return key if value == expected else None


def _row_value(row: Any, key: str, index: int) -> Any:
    try:
        if isinstance(row, dict):
            return row.get(key, None)
        if isinstance(row, (list, tuple)):
            return row[index]
    except (KeyError, IndexError, TypeError):
        return None
    return None


def _decoded_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
