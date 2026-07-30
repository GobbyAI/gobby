"""Canonical structured vote artifacts for interactive plan rounds."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import cast

from gobby.plans.review_evidence_io import _split_snapshot_sections
from gobby.plans.review_evidence_models import PlanReviewEvidence, ReviewEvidenceError

INTERACTION_TOOLS = frozenset({"AskUserQuestion", "request_user_input", "AskUser"})
PLAN_VOTE_INTERACTION_CONTEXT_VARIABLE = "_plan_vote_interaction_context"
PLAN_VOTE_INTERACTION_RECEIPT_VARIABLE = "_plan_vote_interaction_receipt"
ROUND_KINDS = frozenset({"enhancement", "adversary"})
VOTE_DECISIONS = frozenset({"accept", "decline", "defer"})
OBSERVER_PROVENANCE = "observer-captured"
COORDINATOR_PROVENANCE = "coordinator-authored"


def build_plan_vote_artifact(
    *,
    evidence_id: str,
    project_id: str,
    session_id: str,
    plan_path: str,
    round_kind: str,
    round_number: int,
    interaction_tool: str,
    interaction_payload: Mapping[str, object],
    votes: Sequence[Mapping[str, object]],
    provenance: str = OBSERVER_PROVENANCE,
) -> dict[str, object]:
    """Validate and canonicalize one plan-round vote artifact."""
    normalized_path = plan_path.strip().replace("\\", "/")
    if not normalized_path:
        raise _invalid("plan_path must be non-empty")
    if round_kind not in ROUND_KINDS:
        raise _invalid("round_kind must be enhancement or adversary")
    if round_number < 1:
        raise _invalid("round_number must be >= 1")
    if provenance == OBSERVER_PROVENANCE and interaction_tool not in INTERACTION_TOOLS:
        raise _invalid(
            "interaction_tool must be AskUserQuestion, request_user_input, or AskUser; "
            "free-text presentation is not a valid interaction payload"
        )
    if provenance == COORDINATOR_PROVENANCE and interaction_tool != "coordinator_decision":
        raise _invalid("coordinator-authored artifacts require coordinator_decision")
    if provenance not in {OBSERVER_PROVENANCE, COORDINATOR_PROVENANCE}:
        raise _invalid("invalid vote artifact provenance")

    payload = _canonical_object(interaction_payload, owner="interaction_payload")
    presented_items = _presented_items(payload)
    canonical_votes = _canonical_votes(votes, presented_items=presented_items)
    artifact: dict[str, object] = {
        "evidence_id": evidence_id,
        "project_id": project_id,
        "session_id": session_id,
        "plan_path": normalized_path,
        "round_kind": round_kind,
        "round_number": round_number,
        "interaction_tool": interaction_tool,
        "interaction_payload": payload,
        "votes": canonical_votes,
        "provenance": provenance,
    }
    artifact["artifact_id"] = hashlib.sha256(_canonical_bytes(artifact)).hexdigest()
    return artifact


def _presented_items(payload: Mapping[str, object]) -> dict[str, dict[str, str]]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise _invalid("interaction_payload.items must be an array")

    items: dict[str, dict[str, str]] = {}
    for index, raw_item in enumerate(raw_items):
        item = _canonical_object(raw_item, owner=f"interaction_payload.items[{index}]")
        unknown = sorted(
            set(item)
            - {
                "finding_id",
                "target_section_id",
                "full_item_text",
                "proposed_edit_text",
            }
        )
        if unknown:
            raise _invalid(
                f"interaction_payload.items[{index}] has unknown fields: {', '.join(unknown)}"
            )
        finding_id = _required_string(item, "finding_id", f"interaction_payload.items[{index}]")
        target_section_id = _required_string(
            item,
            "target_section_id",
            f"interaction_payload.items[{index}]",
        )
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
            "target_section_id": target_section_id,
            "full_item_text": full_item_text,
            "proposed_edit_text": proposed_edit_text,
        }
    return items


def _canonical_votes(
    votes: Sequence[Mapping[str, object]],
    *,
    presented_items: Mapping[str, Mapping[str, str]],
) -> list[dict[str, str]]:
    if not presented_items and not votes:
        return []
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
            "target_section_id": presented_items[finding_id]["target_section_id"],
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


def validate_observer_receipt(
    receipt: object,
    *,
    evidence_id: str,
    round_number: int,
    round_kind: str,
    content_sha256: str,
    captured_by: str,
    interaction_tool: str,
    interaction_payload: Mapping[str, object],
    votes: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Validate one inline observer receipt and bind votes to its answer."""
    if not isinstance(receipt, Mapping):
        raise ReviewEvidenceError(
            "plan_vote_interaction_not_observed",
            "Record the artifact immediately after the native interaction returns.",
        )
    canonical = _canonical_object(receipt, owner="observer receipt")
    expected = {
        "evidence_id": evidence_id,
        "round_number": round_number,
        "round_kind": round_kind,
        "content_sha256": content_sha256,
        "captured_by": captured_by,
        "tool": interaction_tool,
        "provenance": OBSERVER_PROVENANCE,
    }
    for field, value in expected.items():
        if canonical.get(field) != value:
            raise ReviewEvidenceError(
                "plan_vote_interaction_mismatch",
                f"Observer receipt {field} does not match the evidence-bound vote round.",
            )

    tool_input = canonical.get("tool_input")
    tool_output = canonical.get("tool_output")
    if not isinstance(tool_input, Mapping) or not isinstance(tool_output, Mapping):
        raise ReviewEvidenceError(
            "plan_vote_interaction_payload_mismatch",
            "Observer receipt must contain inline canonical tool_input and tool_output objects.",
        )
    output = _canonical_object(tool_output, owner="observer receipt tool_output")
    if canonical.get("tool_output_sha256") != canonical_digest(output):
        raise ReviewEvidenceError(
            "plan_vote_interaction_payload_mismatch",
            "Observer receipt tool_output digest does not match its inline output.",
        )
    _verify_presented_string_leaves(interaction_payload, tool_input)
    _verify_vote_decisions(votes, output)
    return canonical


def build_coordinator_receipt(
    *,
    evidence_id: str,
    round_number: int,
    round_kind: str,
    content_sha256: str,
    captured_by: str,
    votes: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Build the canonical coordinator-authored decision receipt."""
    decisions = [
        {
            "finding_id": _required_string(vote, "finding_id", "vote"),
            "decision": _required_string(vote, "decision", "vote"),
        }
        for vote in votes
    ]
    receipt: dict[str, object] = {
        "evidence_id": evidence_id,
        "round_number": round_number,
        "round_kind": round_kind,
        "content_sha256": content_sha256,
        "captured_by": captured_by,
        "provenance": COORDINATOR_PROVENANCE,
        "decisions": decisions,
        "decisions_sha256": canonical_digest({"decisions": decisions}),
    }
    return receipt


def validate_accepted_vote_fold_in(
    artifact: Mapping[str, object],
    *,
    section_text: Mapping[str, str],
) -> None:
    """Require every accepted edit in its stable target section."""
    votes = artifact.get("votes")
    if not isinstance(votes, list):
        raise _invalid("stored artifact votes must be an array")
    for raw_vote in votes:
        if not isinstance(raw_vote, Mapping) or raw_vote.get("decision") != "accept":
            continue
        finding_id = _required_string(raw_vote, "finding_id", "stored vote")
        target_section_id = _required_string(raw_vote, "target_section_id", "stored vote")
        proposed_edit_text = _required_string(raw_vote, "proposed_edit_text", "stored vote")
        target_text = section_text.get(target_section_id)
        if target_text is None or _normalize_block(proposed_edit_text) not in _normalize_block(
            target_text
        ):
            raise ReviewEvidenceError(
                "plan_vote_fold_in_mismatch",
                (
                    f"Accepted vote {finding_id} is missing from target section "
                    f"{target_section_id} after fold-in."
                ),
            )


def validate_vote_attempt(
    evidence: PlanReviewEvidence,
    *,
    caller_session_id: str,
    plan_path: str,
    round_number: int,
) -> None:
    if evidence.session_id != caller_session_id:
        raise ReviewEvidenceError(
            "plan_vote_session_mismatch",
            "Caller session does not own the plan review evidence row.",
        )
    if evidence.plan_path != plan_path or evidence.round_number != round_number:
        raise ReviewEvidenceError(
            "wrong_attempt",
            "Vote artifact does not match the evidence plan path and round.",
        )
    if not evidence.is_live:
        raise ReviewEvidenceError(
            "evidence_replay",
            "Inactive evidence cannot record a vote artifact.",
        )


def require_vote_artifact_fold_in(
    evidence: PlanReviewEvidence,
    *,
    plan_bytes: bytes,
) -> None:
    if not evidence.is_interactive:
        return
    artifact = evidence.vote_artifact
    receipt = evidence.vote_receipt
    if (
        artifact is None
        or receipt is None
        or evidence.vote_artifact_digest is None
        or evidence.vote_receipt_digest is None
    ):
        raise ReviewEvidenceError(
            "plan_vote_artifact_required",
            "Interactive plan review finalization requires an evidence-bound vote artifact.",
        )
    if (
        canonical_digest(artifact) != evidence.vote_artifact_digest
        or canonical_digest(receipt) != evidence.vote_receipt_digest
    ):
        raise ReviewEvidenceError(
            "plan_vote_artifact_mismatch",
            "Stored vote artifact or receipt digest is invalid.",
        )
    expected = {
        "evidence_id": evidence.evidence_id,
        "project_id": evidence.project_id,
        "session_id": evidence.session_id,
        "plan_path": evidence.plan_path,
        "round_number": evidence.round_number,
    }
    if any(artifact.get(field) != value for field, value in expected.items()):
        raise ReviewEvidenceError(
            "plan_vote_artifact_mismatch",
            "Stored vote artifact does not match its evidence row.",
        )
    section_text = {
        section_id: content.decode("utf-8")
        for section_id, content in _split_snapshot_sections(plan_bytes)
    }
    validate_accepted_vote_fold_in(artifact, section_text=section_text)


def canonical_digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _verify_presented_string_leaves(
    interaction_payload: Mapping[str, object],
    observed_input: Mapping[str, object],
) -> None:
    observed_leaves = set(_string_leaves(observed_input))
    items = interaction_payload.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ReviewEvidenceError(
            "plan_vote_interaction_payload_mismatch",
            "The artifact interaction items could not be verified.",
        )
    for item in items:
        if not isinstance(item, Mapping):
            raise ReviewEvidenceError(
                "plan_vote_interaction_payload_mismatch",
                "The artifact contains an unverifiable interaction item.",
            )
        for field in (
            "finding_id",
            "target_section_id",
            "full_item_text",
            "proposed_edit_text",
        ):
            value = item.get(field)
            if not isinstance(value, str) or value not in observed_leaves:
                raise ReviewEvidenceError(
                    "plan_vote_interaction_payload_mismatch",
                    f"Observed interaction payload is missing {field} for a recorded item.",
                )


def _verify_vote_decisions(
    votes: Sequence[Mapping[str, object]],
    observed_output: Mapping[str, object],
) -> None:
    observed = _observed_decisions(observed_output)
    for vote in votes:
        finding_id = _required_string(vote, "finding_id", "vote")
        decision = _required_string(vote, "decision", "vote")
        if decision not in observed.get(finding_id, set()):
            raise ReviewEvidenceError(
                "plan_vote_decision_mismatch",
                f"Decision for finding_id {finding_id} is not derivable from observed output.",
            )


def _observed_decisions(raw: object) -> dict[str, set[str]]:
    decisions: dict[str, set[str]] = {}
    if isinstance(raw, Mapping):
        finding_id = raw.get("finding_id")
        decision = raw.get("decision")
        if isinstance(finding_id, str) and isinstance(decision, str):
            decisions.setdefault(finding_id, set()).add(decision)
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, str) and value in VOTE_DECISIONS:
                decisions.setdefault(key, set()).add(value)
            nested = _observed_decisions(value)
            for nested_id, nested_values in nested.items():
                decisions.setdefault(nested_id, set()).update(nested_values)
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            nested = _observed_decisions(item)
            for nested_id, nested_values in nested.items():
                decisions.setdefault(nested_id, set()).update(nested_values)
    return decisions


def _string_leaves(raw: object) -> list[str]:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, Mapping):
        return [leaf for value in raw.values() for leaf in _string_leaves(value)]
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [leaf for value in raw for leaf in _string_leaves(value)]
    return []


def _normalize_block(raw: str) -> str:
    return " ".join(raw.split())


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
