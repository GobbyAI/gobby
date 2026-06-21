"""Guards for persisted feature candidate defaults."""

from __future__ import annotations

import json
import logging
from typing import Any

from gobby.config.feature_base import FeatureProfile, candidate_labels

logger = logging.getLogger(__name__)

_STALE_CANDIDATE_ROW_SOURCES = ("defaults", "one-off-0.5.0-migration")

_OLD_CLAUDE_ONLY_CANDIDATES: dict[FeatureProfile, tuple[str, ...]] = {
    FeatureProfile.LOW: ("claude/haiku",),
    FeatureProfile.MID: ("claude/sonnet",),
    FeatureProfile.HIGH: ("claude/opus",),
}
_OLD_CLAUDE_ONLY_CANDIDATE_ALIASES: dict[FeatureProfile, set[tuple[str, ...]]] = {
    FeatureProfile.HIGH: {
        ("claude/opus",),
        ("claude/claude-opus-4-5",),
    },
}
_OLD_SPARK_CANDIDATE = "codex/gpt-5.3-codex-spark"

_FEATURE_CANDIDATE_PROFILES: dict[str, FeatureProfile] = {
    "ai.generation.profile_defaults.feature_low": FeatureProfile.LOW,
    "ai.generation.profile_defaults.feature_mid": FeatureProfile.MID,
    "ai.generation.profile_defaults.feature_high": FeatureProfile.HIGH,
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
    "gobby_tasks.expansion.candidates": FeatureProfile.HIGH,
    "gobby-tasks.validation.candidates": FeatureProfile.MID,
    "gobby_tasks.validation.candidates": FeatureProfile.MID,
    "chat.candidates": FeatureProfile.HIGH,
    "code_index.summary_candidates": FeatureProfile.LOW,
    "code_index.symbol_summary.candidates": FeatureProfile.LOW,
}


def delete_stale_default_feature_candidate_rows(config_store: Any | None) -> None:
    """Delete old seeded candidates so profile defaults apply."""
    db = getattr(config_store, "db", None)
    if db is None:
        return
    fetchall = getattr(db, "fetchall", None)
    if not callable(fetchall):
        return

    try:
        rows = fetchall(
            "SELECT key, value FROM config_store WHERE source IN (%s, %s)",
            _STALE_CANDIDATE_ROW_SOURCES,
        )
    except Exception as exc:
        logger.debug("Failed to inspect seeded feature candidate rows: %s", exc)
        return

    stale_keys = sorted({key for row in rows if (key := _get_stale_candidate_key(row)) is not None})
    if not stale_keys:
        return

    delete = getattr(config_store, "delete", None)
    if not callable(delete):
        joined = ", ".join(stale_keys)
        raise ValueError(
            "Stale seeded feature candidate rows found in "
            f"config_store but cannot be deleted: {joined}."
        )

    try:
        with db.transaction():
            for key in stale_keys:
                delete(key)
    except Exception as exc:
        logger.debug("Failed to delete stale feature candidate keys %s: %s", stale_keys, exc)
        joined = ", ".join(stale_keys)
        raise ValueError(
            f"Failed to delete stale seeded feature candidate rows from config_store: {joined}."
        ) from exc

    joined = ", ".join(stale_keys)
    logger.warning(
        "Deleted stale seeded feature candidate rows so profile defaults apply: %s",
        joined,
    )


def _get_stale_candidate_key(row: Any) -> str | None:
    key = _row_value(row, "key", 0)
    if not isinstance(key, str) or key not in _FEATURE_CANDIDATE_PROFILES:
        return None
    value = _normalized_candidate_list(_decoded_value(_row_value(row, "value", 1)))
    if value is None:
        return None
    profile = _FEATURE_CANDIDATE_PROFILES[key]
    if any(candidate.startswith("gemini/") for candidate in value):
        return key
    if profile == FeatureProfile.MID and _OLD_SPARK_CANDIDATE in value:
        return key
    candidates = tuple(value)
    expected = _OLD_CLAUDE_ONLY_CANDIDATES[profile]
    aliases = set(_OLD_CLAUDE_ONLY_CANDIDATE_ALIASES.get(profile, ())) | {expected}
    return key if candidates in aliases else None


def _normalized_candidate_list(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple)):
        return None
    try:
        return list(candidate_labels(value))
    except (TypeError, ValueError):
        return None


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
