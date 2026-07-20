"""Deterministic evidence coverage for grounded task validation."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from gobby.ai import InvocationRecord

_CONTENT_KINDS = frozenset({"task_diff", "file_at_commit"})
_METADATA_KINDS = frozenset({"changed_files", "linked_commits"})
RECOVERY_CALL_RESERVE = 4
VERDICT_SUBMISSION_CALLS = 1
MIN_TOOL_CALL_BUDGET = 6
# Claude SDK counts text-only AssistantMessage instances as turns, so tool-call
# plans need independent model-turn headroom beyond the evidence-call budget.
TEXT_TURN_HEADROOM = 2


@dataclass(frozen=True)
class EvidenceGap:
    """One selected artifact whose byte or item coverage is incomplete."""

    selector: dict[str, object]
    total_bytes: int | None
    unconsumed_ranges: tuple[tuple[int, int], ...]
    reason: str
    total_items: int | None = None
    range_unit: str = "bytes"

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "selector": self.selector,
            "unconsumed_ranges": [list(item) for item in self.unconsumed_ranges],
            "reason": self.reason,
        }
        if self.range_unit == "items":
            payload["total_items"] = self.total_items
        else:
            payload["total_bytes"] = self.total_bytes
        return payload


@dataclass(frozen=True)
class EvidenceCoverage:
    """Coverage derived from runtime trace ranges instead of model claims."""

    complete: bool
    evidence_refs: tuple[str, ...]
    content_evidence_refs: tuple[str, ...]
    gaps: tuple[EvidenceGap, ...]
    selected_artifact_count: int

    def error_payload(self) -> dict[str, object] | None:
        if self.complete:
            return None
        code = "no_content_evidence" if not self.selected_artifact_count else "unconsumed_evidence"
        return {
            "code": code,
            "artifacts": [gap.as_dict() for gap in self.gaps],
        }


@dataclass(frozen=True)
class ToolCallPlan:
    """Bounded tool-call allocation with independent SDK turn headroom."""

    max_tool_calls: int
    max_turns: int
    required_tool_calls: int
    diff_pages: int
    manifest_pages: int
    recovery_calls: int
    verdict_calls: int
    within_bound: bool


@dataclass
class _ArtifactCoverage:
    selector: dict[str, object]
    first_index: int
    range_unit: str
    content_hash: str | None = None
    total_bytes: int | None = None
    intervals: list[tuple[int, int]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    complete_without_range: bool = False
    invalid_range: bool = False

    def reset_for_content(self, content_hash: str) -> None:
        if self.content_hash is None:
            self.content_hash = content_hash
            return
        if self.content_hash == content_hash:
            return
        self.content_hash = content_hash
        self.total_bytes = None
        self.intervals.clear()
        self.evidence_refs.clear()
        self.complete_without_range = False
        self.invalid_range = False

    def missing_ranges(self) -> tuple[tuple[int, int], ...]:
        if self.invalid_range or self.total_bytes is None:
            return ()
        cursor = 0
        missing: list[tuple[int, int]] = []
        for start, end in merge_intervals(self.intervals):
            if start > cursor:
                missing.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < self.total_bytes:
            missing.append((cursor, self.total_bytes))
        return tuple(missing)

    @property
    def complete(self) -> bool:
        if self.invalid_range:
            return False
        if self.total_bytes is not None:
            return not self.missing_ranges()
        return self.complete_without_range


def merge_intervals(intervals: Sequence[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    """Return the union of half-open intervals in deterministic order."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)


def plan_tool_calls(
    *,
    diff_total_bytes: int,
    manifest_count: int,
    preview_bytes: int,
    manifest_page_limit: int,
    configured_max_calls: int,
) -> ToolCallPlan:
    """Reserve evidence pages and retry capacity under one configured hard bound."""
    if min(diff_total_bytes, manifest_count, preview_bytes, manifest_page_limit) < 0:
        raise ValueError("validation pagination inputs must be non-negative")
    if preview_bytes == 0 or manifest_page_limit == 0 or configured_max_calls <= 0:
        raise ValueError("validation pagination limits must be positive")

    # Typed envelopes and selectors consume part of the 16 KiB result cap. This
    # conservative capacity keeps the 15,041/15,758 production boundary at two pages.
    effective_page_bytes = max(1, preview_bytes - 2_048)
    diff_pages = max(1, math.ceil(diff_total_bytes / effective_page_bytes))
    manifest_pages = max(1, math.ceil(manifest_count / manifest_page_limit))
    required = diff_pages + manifest_pages + RECOVERY_CALL_RESERVE + VERDICT_SUBMISSION_CALLS
    allocated = min(configured_max_calls, max(MIN_TOOL_CALL_BUDGET, required))
    return ToolCallPlan(
        max_tool_calls=allocated,
        max_turns=2 * allocated + TEXT_TURN_HEADROOM,
        required_tool_calls=required,
        diff_pages=diff_pages,
        manifest_pages=manifest_pages,
        recovery_calls=RECOVERY_CALL_RESERVE,
        verdict_calls=VERDICT_SUBMISSION_CALLS,
        within_bound=required <= configured_max_calls,
    )


def analyze_evidence_coverage(
    trace: Sequence[InvocationRecord], *, require_manifest: bool = False
) -> EvidenceCoverage:
    """Compute selected-artifact coverage from the union of runtime byte ranges."""
    artifacts: dict[str, _ArtifactCoverage] = {}
    aggregate_key: str | None = None

    for index, record in enumerate(trace):
        if not record.get("ok") or record.get("error_code") is not None:
            continue
        selector = _selector(record.get("selector"))
        if selector is None:
            continue
        kind = selector.get("kind")
        evidence_ref = record.get("evidence_ref")
        if kind not in _METADATA_KINDS and kind not in _CONTENT_KINDS:
            continue

        key = _selector_key(selector)
        artifact = artifacts.setdefault(
            key,
            _ArtifactCoverage(
                selector=selector,
                first_index=index,
                range_unit="items" if kind in _METADATA_KINDS else "bytes",
            ),
        )
        content_hash = record.get("content_hash")
        if isinstance(content_hash, str):
            artifact.reset_for_content(content_hash)
        if isinstance(evidence_ref, str):
            artifact.evidence_refs.append(evidence_ref)
        selected_range = (
            _cursor_range(record.get("range"))
            if kind in _METADATA_KINDS
            else _byte_range(record.get("range"))
        )
        if selected_range is None:
            artifact.complete_without_range = record.get("complete") is True
        else:
            start, end, total = selected_range
            if start < 0 or end < start or total < end:
                artifact.invalid_range = True
            else:
                if artifact.total_bytes is not None and artifact.total_bytes != total:
                    artifact.invalid_range = True
                artifact.total_bytes = total
                artifact.intervals.append((start, end))

        if _is_aggregate_task_diff(selector) and artifact.complete:
            aggregate_key = key

    relevant: list[_ArtifactCoverage] = []
    for key, artifact in artifacts.items():
        kind = artifact.selector.get("kind")
        if aggregate_key is not None and kind == "task_diff" and key != aggregate_key:
            continue
        if aggregate_key is not None and kind == "file_at_commit":
            continue
        relevant.append(artifact)

    manifest_selected = any(
        artifact.selector.get("kind") == "changed_files" for artifact in relevant
    )
    if require_manifest and not manifest_selected:
        relevant.append(
            _ArtifactCoverage(
                selector={"kind": "changed_files"},
                first_index=-1,
                range_unit="items",
            )
        )

    gaps: list[EvidenceGap] = []
    complete_refs: list[str] = []
    content_refs: list[str] = []
    content_artifact_count = 0
    for artifact in sorted(relevant, key=lambda item: item.first_index):
        is_content = artifact.selector.get("kind") in _CONTENT_KINDS
        if is_content:
            content_artifact_count += 1
        if artifact.complete:
            complete_refs.extend(artifact.evidence_refs)
            if is_content:
                content_refs.extend(artifact.evidence_refs)
            continue
        reason = "invalid_range" if artifact.invalid_range else "range_incomplete"
        gaps.append(
            EvidenceGap(
                selector=artifact.selector,
                total_bytes=artifact.total_bytes if artifact.range_unit == "bytes" else None,
                unconsumed_ranges=artifact.missing_ranges(),
                reason=reason,
                total_items=artifact.total_bytes if artifact.range_unit == "items" else None,
                range_unit=artifact.range_unit,
            )
        )

    if not content_artifact_count:
        gaps.append(
            EvidenceGap(
                selector={"kind": "task_diff"},
                total_bytes=None,
                unconsumed_ranges=(),
                reason="not_selected",
            )
        )
    return EvidenceCoverage(
        complete=bool(content_artifact_count) and not gaps,
        evidence_refs=tuple(dict.fromkeys(complete_refs)),
        content_evidence_refs=tuple(dict.fromkeys(content_refs)),
        gaps=tuple(gaps),
        selected_artifact_count=content_artifact_count,
    )


def _selector(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    selector = {key: item for key, item in value.items() if isinstance(key, str)}
    return selector if isinstance(selector.get("kind"), str) else None


def _selector_key(selector: Mapping[str, object]) -> str:
    return json.dumps(selector, sort_keys=True, separators=(",", ":"), default=str)


def _byte_range(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, Mapping):
        return None
    start = value.get("byte_start")
    end = value.get("byte_end")
    total = value.get("total_bytes")
    if not isinstance(start, int) or isinstance(start, bool):
        return None
    if not isinstance(end, int) or isinstance(end, bool):
        return None
    if not isinstance(total, int) or isinstance(total, bool):
        return None
    return start, end, total


def _cursor_range(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, Mapping):
        return None
    start = value.get("cursor_offset")
    end = value.get("cursor_end")
    total = value.get("total")
    if not isinstance(start, int) or isinstance(start, bool):
        return None
    if not isinstance(end, int) or isinstance(end, bool):
        return None
    if not isinstance(total, int) or isinstance(total, bool):
        return None
    return start, end, total


def _is_aggregate_task_diff(selector: Mapping[str, object]) -> bool:
    return (
        selector.get("kind") == "task_diff"
        and "commit" not in selector
        and "path_selector" not in selector
    )
