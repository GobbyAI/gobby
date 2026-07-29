"""Canonical structured vote artifacts for interactive plan rounds."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import cast

from gobby.plans.review_evidence_models import ReviewEvidenceError

INTERACTION_TOOLS = frozenset({"AskUserQuestion", "request_user_input", "AskUser"})
PLAN_VOTE_INTERACTION_RECEIPT_VARIABLE = "_plan_vote_interaction_receipt"
ROUND_KINDS = frozenset({"enhancement", "adversary"})
VOTE_DECISIONS = frozenset({"accept", "decline", "defer"})


def build_plan_vote_artifact(
    *,
    project_id: str,
    session_id: str,
    plan_path: str,
    round_kind: str,
    round_number: int,
    interaction_tool: str,
    interaction_payload: Mapping[str, object],
    votes: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Validate and canonicalize one interactive plan-round vote artifact."""
    normalized_path = plan_path.strip().replace("\\", "/")
    if not normalized_path:
        raise _invalid("plan_path must be non-empty")
    if round_kind not in ROUND_KINDS:
        raise _invalid("round_kind must be enhancement or adversary")
    if round_number < 1:
        raise _invalid("round_number must be >= 1")
    if interaction_tool not in INTERACTION_TOOLS:
        raise _invalid(
            "interaction_tool must be AskUserQuestion, request_user_input, or AskUser; "
            "free-text presentation is not a valid interaction payload"
        )

    payload = _canonical_object(interaction_payload, owner="interaction_payload")
    presented_items = _presented_items(payload)
    canonical_votes = _canonical_votes(votes, presented_items=presented_items)
    artifact: dict[str, object] = {
        "project_id": project_id,
        "session_id": session_id,
        "plan_path": normalized_path,
        "round_kind": round_kind,
        "round_number": round_number,
        "interaction_tool": interaction_tool,
        "interaction_payload": payload,
        "votes": canonical_votes,
    }
    artifact["artifact_id"] = hashlib.sha256(_canonical_bytes(artifact)).hexdigest()
    return artifact


def _presented_items(payload: Mapping[str, object]) -> dict[str, dict[str, str]]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise _invalid("interaction_payload.items must be a non-empty array")

    items: dict[str, dict[str, str]] = {}
    for index, raw_item in enumerate(raw_items):
        item = _canonical_object(raw_item, owner=f"interaction_payload.items[{index}]")
        unknown = sorted(set(item) - {"finding_id", "full_item_text", "proposed_edit_text"})
        if unknown:
            raise _invalid(
                f"interaction_payload.items[{index}] has unknown fields: {', '.join(unknown)}"
            )
        finding_id = _required_string(item, "finding_id", f"interaction_payload.items[{index}]")
        if finding_id in items:
            raise _invalid(f"duplicate presented finding_id: {finding_id}")
        full_item_text = _required_string(
            item,
            "full_item_text",
            f"interaction_payload.items[{index}]",
        )
        proposed_edit_text = _required_string(
            item,
            "proposed_edit_text",
            f"interaction_payload.items[{index}]",
        )
        if proposed_edit_text not in full_item_text:
            raise _invalid(
                f"full_item_text must contain proposed_edit_text for finding_id {finding_id}"
            )
        items[finding_id] = {
            "finding_id": finding_id,
            "full_item_text": full_item_text,
            "proposed_edit_text": proposed_edit_text,
        }
    return items


def _canonical_votes(
    votes: Sequence[Mapping[str, object]],
    *,
    presented_items: Mapping[str, Mapping[str, str]],
) -> list[dict[str, str]]:
    if not votes:
        raise _invalid("votes must contain one explicit decision per presented item")

    by_finding: dict[str, dict[str, str]] = {}
    vote_ids: set[str] = set()
    for index, raw_vote in enumerate(votes):
        vote = _canonical_object(raw_vote, owner=f"votes[{index}]")
        unknown = sorted(set(vote) - {"vote_id", "finding_id", "decision"})
        if unknown:
            raise _invalid(f"votes[{index}] has unknown fields: {', '.join(unknown)}")
        vote_id = _required_string(vote, "vote_id", f"votes[{index}]")
        finding_id = _required_string(vote, "finding_id", f"votes[{index}]")
        decision = _required_string(vote, "decision", f"votes[{index}]")
        if decision not in VOTE_DECISIONS:
            raise _invalid(f"invalid decision for finding_id {finding_id}: {decision}")
        if vote_id in vote_ids:
            raise _invalid(f"duplicate vote_id: {vote_id}")
        if finding_id in by_finding:
            raise _invalid(f"duplicate vote for finding_id: {finding_id}")
        if finding_id not in presented_items:
            raise _invalid(f"vote references unpresented finding_id: {finding_id}")
        vote_ids.add(vote_id)
        by_finding[finding_id] = {
            "vote_id": vote_id,
            "finding_id": finding_id,
            "decision": decision,
            "proposed_edit_text": presented_items[finding_id]["proposed_edit_text"],
        }

    missing = sorted(set(presented_items) - set(by_finding))
    if missing:
        raise _invalid(
            "missing per-item votes for finding_id: "
            + ", ".join(missing)
            + "; blanket decisions are invalid"
        )
    return [by_finding[finding_id] for finding_id in presented_items]


def _canonical_object(raw: object, *, owner: str) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise _invalid(f"{owner} must be an object")
    try:
        encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise _invalid(f"{owner} must contain JSON values") from exc
    if not isinstance(decoded, dict):
        raise _invalid(f"{owner} must be an object")
    return cast(dict[str, object], decoded)


def _required_string(payload: Mapping[str, object], field: str, owner: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{owner}.{field} must be a non-empty string")
    return value.strip()


def _canonical_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _invalid(message: str) -> ReviewEvidenceError:
    return ReviewEvidenceError("invalid_plan_vote_artifact", message)
