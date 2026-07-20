"""Prompt construction for exhaustive and bounded task validation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _build_prompt(
    *,
    title: str,
    description: str | None,
    validation_criteria: str | None,
    category: str | None,
    commit_count: int,
    first_commits_page: Mapping[str, object],
    manifest_count: int,
    diff_total_bytes: int,
    mode: Literal["exhaustive", "bounded"],
    content_call_budget: int,
    verification_item_count: int,
) -> str:
    criteria_label = "Validation criteria" if validation_criteria else "Task description"
    criteria = validation_criteria or description or ""
    category_line = f"Task category: {category}\n" if category else ""
    if mode == "bounded":
        acquisition = (
            "This is bounded evidence mode. List the complete changed-file manifest in maximum-sized "
            f"pages, then spend at most {content_call_budget} content calls on risk-ranked per-file "
            "inspection. Rank validation-criteria relevance first, then change magnitude "
            "(lines_added + lines_deleted; null counts mean binary). Skip generated files and "
            "lockfiles unless the criteria make them relevant. read_task_diff calls must include "
            "both commit and path_selector; aggregate and per-commit views return "
            "bounded_view_forbidden. Fully page every selected view before submitting a verdict. "
            "The server computes and discloses uninspected files. Submit evidence_complete=false; "
            "evidence_complete=true is rejected."
        )
    else:
        acquisition = (
            "Follow this acquisition order exactly: list the changed-file manifest in maximum-sized "
            "pages, then read the aggregate task diff without commit or path_selector from byte zero "
            "forward. Continue at each returned byte_end until total_bytes is covered. Treat byte "
            "coverage as the union of half-open ranges; overlap never advances coverage. Retry the "
            "same range after snapshot_required or view_changed. Once the aggregate task diff is "
            "complete, do not search the repository or acquire whole files."
        )
    if verification_item_count:
        verification = (
            f"{verification_item_count} runtime-recorded command evidence items are available. "
            "Use list_verification_evidence to inspect command results with exit codes and cite the "
            "evidence_ref values from completed pages you rely on."
        )
    else:
        verification = "No runtime-recorded command evidence items are available."
    return (
        "Validate completion using only runtime-issued evidence from the paged tools.\n"
        f"{acquisition} Cite only evidence_ref values returned by successful tool invocations.\n"
        f"{verification}\n"
        "Finish by calling submit_validation_verdict with status, feedback, blocking_reasons, "
        "current_failure_evidence, evidence_refs, and evidence_complete. status must be valid, "
        "invalid, or pending. current_failure_evidence must contain one entry for each currently "
        "failing state you attest exists, and must be an empty array when nothing is currently "
        "failing. A current failure does not include TDD red-phase history, quoted failure "
        "examples, failure-handling code such as FAILED=1, or status values named failed. Cite "
        "every completed evidence page used. If a contradictory submission is rejected, correct "
        "and resubmit it: either return status='invalid' with blocking_reasons, or return an empty "
        "current_failure_evidence array if nothing is currently failing. After the submission is "
        "accepted, emit no narrative or additional JSON.\n\n"
        f"Task title: {title}\n"
        f"{category_line}"
        f"{criteria_label}:\n{criteria}\n\n"
        f"Linked commit count: {commit_count}\n"
        f"First linked-commits page: {_compact_json(dict(first_commits_page))}\n"
        f"Changed-file manifest count: {manifest_count}\n\n"
        f"Aggregate task-diff bytes: {diff_total_bytes}"
    )
