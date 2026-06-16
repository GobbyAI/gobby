"""Dream plan validation.

The sweep verdicts are per-memory: ``keep | delete | refresh | review | promote``.
Only an explicit, valid ``review`` or ``delete`` hides a memory; ``refresh``
rewrites it in place; ``promote`` moves a repo-scoped universal memory to global
scope; ``keep`` leaves it visible. Anything malformed, omitted, unknown, or below
the confidence threshold degrades to a visible ``keep`` carrying the reason, so a
planner failure can never silently hide or rescope a memory.

Legacy ``merge``/``supersede`` verdicts are no longer emitted (cross-memory
consolidation is out of scope for the GC sweep); if a planner still returns one
it is treated as an unknown action and degraded to ``keep``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from gobby.memory.dream.models import DreamAction, DreamActionName, DreamCandidate

_VALID_ACTIONS = {"keep", "delete", "refresh", "review", "promote"}
# Verdicts that change a row and therefore must clear the confidence bar before
# they apply. ``review`` and ``delete`` hide the memory; ``refresh`` rewrites it;
# ``promote`` changes scope.
_MUTATING_ACTIONS = {"delete", "refresh", "review", "promote"}


def validate_dream_plan(
    raw_plan: Any,
    candidates: list[DreamCandidate],
    *,
    min_action_confidence: float,
    min_delete_confidence: float,
    min_rescope_confidence: float,
) -> list[DreamAction]:
    """Validate raw planner output, degrading anything unsafe to visible keep."""
    candidate_ids = {candidate.id for candidate in candidates}
    touched: set[str] = set()
    actions: list[DreamAction] = []
    raw_actions = _extract_raw_actions(raw_plan)

    for item in raw_actions:
        if not isinstance(item, Mapping):
            continue
        action = _coerce_action_name(item)
        refs = _referenced_candidate_ids(item)
        valid_refs = refs & candidate_ids
        invalid_refs = refs - candidate_ids

        if action not in _VALID_ACTIONS:
            actions.extend(_keep_each(valid_refs, "unknown action", item))
            touched.update(valid_refs)
            continue
        if invalid_refs:
            if valid_refs:
                actions.extend(_keep_each(valid_refs, "unknown candidate id", item))
            else:
                actions.append(_keep_marker("unknown candidate id", _confidence(item)))
            touched.update(valid_refs)
            continue
        if not valid_refs:
            actions.append(_keep_marker("missing candidate id", _confidence(item)))
            continue
        overlapping_refs = valid_refs & touched
        if overlapping_refs:
            actions.extend(
                _keep_each(overlapping_refs, "candidate had overlapping dream actions", item)
            )
            valid_refs -= overlapping_refs
        if not valid_refs:
            continue
        if action in _MUTATING_ACTIONS and not _confidence_ok(
            action,
            _confidence(item),
            min_action_confidence=min_action_confidence,
            min_delete_confidence=min_delete_confidence,
            min_rescope_confidence=min_rescope_confidence,
        ):
            actions.extend(_keep_each(valid_refs, "confidence below mutation threshold", item))
            touched.update(valid_refs)
            continue

        actions.extend(_validate_action(action, valid_refs, item))
        touched.update(valid_refs)

    omitted = candidate_ids - touched
    actions.extend(
        DreamAction(
            action="keep",
            memory_id=memory_id,
            reason="candidate omitted from dream plan",
            confidence=0.0,
        )
        for memory_id in sorted(omitted)
    )
    return _dedupe_actions(actions)


def _extract_raw_actions(raw_plan: Any) -> list[Any]:
    if isinstance(raw_plan, str):
        try:
            raw_plan = json.loads(raw_plan)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw_plan, Mapping):
        return []
    raw_actions = raw_plan.get("actions", raw_plan.get("memories", []))
    return list(raw_actions) if isinstance(raw_actions, list) else []


def _coerce_action_name(item: Mapping[str, Any]) -> str:
    action = item.get("action", item.get("verdict", "keep"))
    return str(action).strip().lower()


def _referenced_candidate_ids(item: Mapping[str, Any]) -> set[str]:
    memory_id = item.get("memory_id", item.get("id"))
    return {str(memory_id)} if memory_id else set()


def _confidence(item: Mapping[str, Any]) -> float:
    raw = item.get("confidence", 0.0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _confidence_ok(
    action: str,
    confidence: float,
    *,
    min_action_confidence: float,
    min_delete_confidence: float,
    min_rescope_confidence: float,
) -> bool:
    if action == "delete":
        threshold = min_delete_confidence
    elif action == "promote":
        threshold = min_rescope_confidence
    else:
        threshold = min_action_confidence
    return confidence >= threshold


def _validate_action(
    action: str,
    refs: set[str],
    item: Mapping[str, Any],
) -> list[DreamAction]:
    confidence = _confidence(item)
    reason = str(item.get("reason", "") or "")
    memory_id = next(iter(refs))
    if action == "refresh" and not _content(item):
        return _keep_each(refs, "refresh requires replacement content", item)
    return [
        DreamAction(
            action=cast(DreamActionName, action),
            memory_id=memory_id,
            content=_content(item),
            memory_type=str(item["memory_type"]) if item.get("memory_type") else None,
            tags=_tags(item),
            reason=reason,
            confidence=confidence,
        )
    ]


def _content(item: Mapping[str, Any]) -> str | None:
    value = item.get("content", item.get("new_content", item.get("canonical_content")))
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _tags(item: Mapping[str, Any]) -> list[str] | None:
    value = item.get("tags")
    if not isinstance(value, list):
        return None
    return [str(tag) for tag in value if str(tag).strip()]


def _keep_each(
    refs: set[str],
    reason: str,
    item: Mapping[str, Any],
) -> list[DreamAction]:
    return [
        DreamAction(
            action="keep",
            memory_id=memory_id,
            reason=reason,
            confidence=_confidence(item),
        )
        for memory_id in sorted(refs)
    ]


def _keep_marker(reason: str, confidence: float) -> DreamAction:
    """Audit-only keep with no candidate id (planner referenced unknown ids)."""
    return DreamAction(action="keep", reason=reason, confidence=confidence)


def _dedupe_actions(actions: list[DreamAction]) -> list[DreamAction]:
    seen: set[str] = set()
    result: list[DreamAction] = []
    for action in actions:
        refs = action.affected_ids()
        if refs & seen:
            if action.action == "keep":
                result.append(action)
                continue
            for memory_id in sorted(refs - seen):
                result.append(
                    DreamAction(
                        action="keep",
                        memory_id=memory_id,
                        reason="candidate had overlapping dream actions",
                        confidence=0.0,
                    )
                )
                seen.add(memory_id)
            continue
        result.append(action)
        seen.update(refs)
    return result
