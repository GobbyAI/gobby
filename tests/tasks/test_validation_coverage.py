"""Regression contracts for deterministic grounded-validation completion."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from gobby.ai import BuiltinExecutionContext, InvocationRecord, ToolChatResult
from gobby.tasks.diff_manifest import ManifestItem
from gobby.tasks.validation_coverage import (
    analyze_evidence_coverage,
    compute_bounded_disclosure,
    plan_tool_calls,
)
from gobby.tasks.validation_tool_loop import (
    ValidationVerdictSink,
    build_validation_builtins,
    normalize_tool_loop_result,
)

COMMIT = "a" * 40


def _page(
    *,
    kind: str = "task_diff",
    start: int,
    end: int,
    total: int,
    ref: str,
    path_selector: str | None = None,
) -> InvocationRecord:
    selector: dict[str, object] = {"kind": kind, "task_id": "task-1"}
    if path_selector is not None:
        selector["path_selector"] = path_selector
    if kind == "file_at_commit":
        selector["commit"] = COMMIT
    return {
        "tool_name": "read_file_at_commit" if kind == "file_at_commit" else "read_task_diff",
        "arguments": {"offset_bytes": start},
        "result_size_bytes": end - start,
        "ok": True,
        "error_code": None,
        "evidence_ref": ref,
        "selector": selector,
        "range": {"byte_start": start, "byte_end": end, "total_bytes": total},
        "complete": end == total,
        "content_hash": "snapshot",
    }


def _manifest_page(*, start: int, end: int, total: int, ref: str) -> InvocationRecord:
    return {
        "tool_name": "list_changed_files",
        "arguments": {"offset": start},
        "result_size_bytes": end - start,
        "ok": True,
        "error_code": None,
        "evidence_ref": ref,
        "selector": {"kind": "changed_files", "task_id": "task-1"},
        "range": {"cursor_offset": start, "cursor_end": end, "total": total},
        "complete": end == total,
        "content_hash": "snapshot",
    }


def _manifest_item(
    path: str,
    selector: str,
    *,
    role: str | None = None,
    status: str = "M",
) -> ManifestItem:
    item: ManifestItem = {
        "commit": COMMIT,
        "status": status,
        "path": {"encoding": "utf-8", "text": path},
        "path_selector": selector,
    }
    if role == "old" or role == "new":
        item["role"] = role
    return item


def _retry_error(code: str) -> InvocationRecord:
    return {
        "tool_name": "read_file_at_commit",
        "arguments": {"offset_bytes": 0},
        "result_size_bytes": 64,
        "ok": False,
        "error_code": code,
        "evidence_ref": None,
    }


def _valid_result(
    trace: tuple[InvocationRecord, ...], refs: list[str], *, budget_exhausted: bool = False
) -> ToolChatResult:
    return ToolChatResult(
        text=json.dumps(
            {
                "status": "valid",
                "feedback": "Every criterion is grounded in the completed diff.",
                "blocking_reasons": [],
                "current_failure_evidence": [],
                "evidence_refs": refs,
                "evidence_complete": True,
            }
        ),
        trace=trace,
        calls_used=len(trace),
        budget_exhausted=budget_exhausted,
        trace_available=True,
    )


def test_two_complete_file_diffs_finish_with_typed_verdict() -> None:
    trace = (
        _page(start=0, end=997, total=997, ref="ev_a", path_selector="a.py"),
        _page(start=0, end=569, total=569, ref="ev_b", path_selector="b.py"),
    )

    verdict = normalize_tool_loop_result(
        _valid_result(trace, ["ev_a", "ev_b"], budget_exhausted=True)
    )

    assert verdict.status == "valid"
    assert verdict.evidence_complete is True
    assert verdict.evidence_refs == ("ev_a", "ev_b")
    assert {record["tool_name"] for record in trace} == {"read_task_diff"}


def test_wide_manifest_uses_derived_bounded_batch_plan() -> None:
    plan = plan_tool_calls(
        diff_total_bytes=100_000,
        manifest_count=104,
        preview_bytes=16_384,
        manifest_page_limit=200,
        configured_max_calls=32,
    )

    assert plan.diff_pages == 7
    assert plan.manifest_pages == 1
    assert plan.recovery_calls == 4
    assert plan.verdict_calls == 1
    assert plan.required_tool_calls == 13
    assert plan.max_tool_calls == 13
    assert plan.max_turns == 28
    assert plan.within_bound is True
    assert plan.mode == "exhaustive"
    assert plan.content_call_budget == 7


def test_over_budget_plan_derives_turns_from_capped_allocation() -> None:
    plan = plan_tool_calls(
        diff_total_bytes=1_000_000,
        manifest_count=4,
        preview_bytes=16_384,
        manifest_page_limit=200,
        configured_max_calls=32,
    )

    assert plan.within_bound is False
    assert plan.max_tool_calls == 32
    assert plan.max_turns == 66
    assert plan.mode == "bounded"
    assert plan.content_call_budget == 26


def test_smallest_derived_plan_grants_turn_headroom() -> None:
    plan = plan_tool_calls(
        diff_total_bytes=1,
        manifest_count=1,
        preview_bytes=16_384,
        manifest_page_limit=200,
        configured_max_calls=32,
    )

    assert plan.max_tool_calls == 7
    assert plan.max_turns == 16


def test_oversized_production_shape_uses_bounded_content_budget() -> None:
    plan = plan_tool_calls(
        diff_total_bytes=1_078_130,
        manifest_count=4,
        preview_bytes=16_384,
        manifest_page_limit=200,
        configured_max_calls=32,
    )

    assert plan.required_tool_calls == 82
    assert plan.mode == "bounded"
    assert plan.content_call_budget == 26
    assert plan.max_turns == 66


def test_large_manifest_without_minimum_content_budget_is_infeasible() -> None:
    plan = plan_tool_calls(
        diff_total_bytes=1_000_000,
        manifest_count=3_900,
        preview_bytes=16_384,
        manifest_page_limit=200,
        configured_max_calls=32,
    )

    assert plan.mode == "infeasible"
    assert plan.content_call_budget == 7


def test_content_budget_clamps_to_zero() -> None:
    plan = plan_tool_calls(
        diff_total_bytes=1_000_000,
        manifest_count=6_000,
        preview_bytes=16_384,
        manifest_page_limit=200,
        configured_max_calls=32,
    )

    assert plan.mode == "infeasible"
    assert plan.content_call_budget == 0


def test_verification_page_is_reserved_in_exhaustive_and_bounded_plans() -> None:
    exhaustive = plan_tool_calls(
        diff_total_bytes=1,
        manifest_count=1,
        preview_bytes=16_384,
        manifest_page_limit=200,
        configured_max_calls=32,
        verification_pages=1,
    )
    bounded = plan_tool_calls(
        diff_total_bytes=1_000_000,
        manifest_count=1,
        preview_bytes=16_384,
        manifest_page_limit=200,
        configured_max_calls=32,
        verification_pages=1,
    )

    assert exhaustive.required_tool_calls == 8
    assert bounded.mode == "bounded"
    assert bounded.content_call_budget == 25


def test_bounded_disclosure_partitions_complete_content_artifacts() -> None:
    manifest = (
        _manifest_item("a.py", "a"),
        _manifest_item("b.py", "b"),
        _manifest_item("c.py", "c"),
    )
    trace = (
        _page(start=0, end=10, total=10, ref="ev_a", path_selector="a"),
        _page(start=0, end=5, total=10, ref="ev_b", path_selector="b"),
    )

    summary = compute_bounded_disclosure(trace, manifest_items=manifest, content_call_budget=26)

    assert summary.manifest_total == 3
    assert summary.inspected_paths == ("a.py",)
    assert summary.uninspected_sample == ("b.py", "c.py")
    assert summary.as_dict()["inspected_count"] == 1


def test_bounded_disclosure_pairs_renames_and_accepts_file_artifacts() -> None:
    manifest = (
        _manifest_item("old.py", "old", role="old", status="R100"),
        _manifest_item("new.py", "new", role="new", status="R100"),
    )
    trace = (
        _page(
            kind="file_at_commit",
            start=0,
            end=4,
            total=4,
            ref="ev_old",
            path_selector="old",
        ),
    )

    summary = compute_bounded_disclosure(trace, manifest_items=manifest, content_call_budget=8)

    assert summary.manifest_total == 1
    assert summary.inspected_count == 1
    assert summary.inspected_paths == ("old.py -> new.py",)


def test_bounded_disclosure_caps_uninspected_sample() -> None:
    manifest = tuple(_manifest_item(f"file-{index}.py", str(index)) for index in range(25))

    summary = compute_bounded_disclosure((), manifest_items=manifest, content_call_budget=8)

    assert summary.uninspected_count == 25
    assert len(summary.uninspected_sample) == 20


def test_wide_manifest_must_cover_every_changed_file_in_production() -> None:
    trace = (
        _manifest_page(start=0, end=100, total=104, ref="ev_manifest_1"),
        _page(start=0, end=997, total=997, ref="ev_diff"),
    )
    result = _valid_result(trace, ["ev_diff"])

    coverage = analyze_evidence_coverage(trace, require_manifest=True)
    verdict = normalize_tool_loop_result(
        result,
        submitted_verdict=json.loads(result.text),
        require_submission=True,
    )

    assert coverage.complete is False
    assert coverage.gaps[0].selector["kind"] == "changed_files"
    assert coverage.gaps[0].total_items == 104
    assert coverage.gaps[0].unconsumed_ranges == ((100, 104),)
    assert verdict.status == "pending"
    assert verdict.evidence_error == {
        "code": "unconsumed_evidence",
        "artifacts": [
            {
                "selector": {"kind": "changed_files", "task_id": "task-1"},
                "unconsumed_ranges": [[100, 104]],
                "reason": "range_incomplete",
                "total_items": 104,
            }
        ],
    }


def test_production_requires_manifest_selection_before_terminal_verdict() -> None:
    trace = (_page(start=0, end=997, total=997, ref="ev_diff"),)
    result = _valid_result(trace, ["ev_diff"])

    verdict = normalize_tool_loop_result(
        result,
        submitted_verdict=json.loads(result.text),
        require_submission=True,
    )

    assert verdict.status == "pending"
    assert verdict.evidence_error == {
        "code": "unconsumed_evidence",
        "artifacts": [
            {
                "selector": {"kind": "changed_files"},
                "unconsumed_ranges": [],
                "reason": "range_incomplete",
                "total_items": None,
            }
        ],
    }


def test_verification_metadata_cannot_replace_content_grounding() -> None:
    verification_page: InvocationRecord = {
        "tool_name": "list_verification_evidence",
        "arguments": {"offset": 0},
        "result_size_bytes": 128,
        "ok": True,
        "error_code": None,
        "evidence_ref": "ev_commands",
        "selector": {"kind": "verification_evidence", "task_id": "task-1"},
        "range": {"cursor_offset": 0, "cursor_end": 1, "total": 1},
        "complete": True,
    }
    trace = (
        _manifest_page(start=0, end=1, total=1, ref="ev_manifest"),
        _page(start=0, end=10, total=10, ref="ev_diff"),
        verification_page,
    )
    payload = {
        "status": "valid",
        "feedback": "Commands passed.",
        "blocking_reasons": [],
        "current_failure_evidence": [],
        "evidence_refs": ["ev_commands"],
        "evidence_complete": True,
    }

    coverage = analyze_evidence_coverage(trace, require_manifest=True)
    verdict = normalize_tool_loop_result(
        ToolChatResult(text="", trace=trace, trace_available=True),
        submitted_verdict=payload,
        require_submission=True,
    )

    assert "ev_commands" in coverage.evidence_refs
    assert "ev_commands" not in coverage.content_evidence_refs
    assert verdict.status == "pending"
    assert verdict.evidence_refs == ("ev_commands",)


@pytest.mark.parametrize(
    ("total_bytes", "expected_pages"),
    ((15_758, 2), (21_591, 2), (155_313, 11)),
)
def test_page_plan_reserves_every_known_continuation(total_bytes: int, expected_pages: int) -> None:
    plan = plan_tool_calls(
        diff_total_bytes=total_bytes,
        manifest_count=4,
        preview_bytes=16_384,
        manifest_page_limit=200,
        configured_max_calls=32,
    )

    assert plan.diff_pages == expected_pages
    assert plan.required_tool_calls == expected_pages + 1 + 4 + 1
    assert plan.max_turns == 2 * plan.max_tool_calls + 2
    assert plan.within_bound is True


def test_selected_file_reports_exact_second_page_gap() -> None:
    trace = tuple(
        _page(start=0, end=size, total=size, ref=f"ev_{index}", path_selector=f"f{index}.py")
        for index, size in enumerate((614, 6_004, 2_384, 900))
    ) + (
        _page(
            kind="file_at_commit",
            start=0,
            end=15_041,
            total=15_758,
            ref="ev_file",
            path_selector="source.py",
        ),
    )

    coverage = analyze_evidence_coverage(trace)

    assert coverage.complete is False
    assert coverage.gaps[0].unconsumed_ranges == ((15_041, 15_758),)


def test_fourth_diff_reports_exact_6442_byte_gap() -> None:
    trace = (
        _page(start=0, end=614, total=614, ref="ev_1", path_selector="a.py"),
        _page(start=0, end=6_004, total=6_004, ref="ev_2", path_selector="b.py"),
        _page(start=0, end=2_384, total=2_384, ref="ev_3", path_selector="c.py"),
        _page(start=0, end=15_149, total=21_591, ref="ev_4", path_selector="d.py"),
    )

    coverage = analyze_evidence_coverage(trace)

    assert coverage.complete is False
    assert coverage.gaps[0].unconsumed_ranges == ((15_149, 21_591),)
    assert coverage.error_payload() == {
        "code": "unconsumed_evidence",
        "artifacts": [coverage.gaps[0].as_dict()],
    }


def test_complete_aggregate_diff_supersedes_later_redundant_file_reads() -> None:
    trace = (
        _page(start=0, end=2_546, total=2_546, ref="ev_diff"),
        _retry_error("snapshot_required"),
        _retry_error("view_changed"),
        _page(
            kind="file_at_commit",
            start=0,
            end=4,
            total=155_313,
            ref="ev_file_1",
            path_selector="large.py",
        ),
        _page(
            kind="file_at_commit",
            start=125_000,
            end=135_000,
            total=155_313,
            ref="ev_file_2",
            path_selector="large.py",
        ),
        _page(
            kind="file_at_commit",
            start=135_000,
            end=145_000,
            total=155_313,
            ref="ev_file_3",
            path_selector="large.py",
        ),
        _page(
            kind="file_at_commit",
            start=140_000,
            end=150_000,
            total=155_313,
            ref="ev_file_4",
            path_selector="large.py",
        ),
        _page(
            kind="file_at_commit",
            start=143_500,
            end=153_500,
            total=155_313,
            ref="ev_file_5",
            path_selector="large.py",
        ),
    )

    coverage = analyze_evidence_coverage(trace)
    verdict = normalize_tool_loop_result(_valid_result(trace, ["ev_diff"], budget_exhausted=True))

    assert coverage.complete is True
    assert coverage.evidence_refs == ("ev_diff",)
    assert verdict.status == "valid"


def test_complete_aggregate_diff_supersedes_earlier_redundant_file_read() -> None:
    trace = (
        _page(
            kind="file_at_commit",
            start=0,
            end=4,
            total=155_313,
            ref="ev_file",
            path_selector="large.py",
        ),
        _page(start=0, end=2_546, total=2_546, ref="ev_diff"),
    )

    coverage = analyze_evidence_coverage(trace)

    assert coverage.complete is True
    assert coverage.evidence_refs == ("ev_diff",)


def test_overlapping_out_of_order_ranges_do_not_fake_full_coverage() -> None:
    trace = tuple(
        _page(
            kind="file_at_commit",
            start=start,
            end=end,
            total=155_313,
            ref=f"ev_{index}",
            path_selector="large.py",
        )
        for index, (start, end) in enumerate(
            ((0, 4), (125_000, 135_000), (135_000, 145_000), (140_000, 150_000), (143_500, 153_500))
        )
    )

    coverage = analyze_evidence_coverage(trace)

    assert coverage.complete is False
    assert coverage.gaps[0].unconsumed_ranges == ((4, 125_000), (153_500, 155_313))


def test_incomplete_trace_returns_typed_acquisition_error_before_model_claim() -> None:
    trace = (_page(start=0, end=15_149, total=21_591, ref="ev_partial"),)

    verdict = normalize_tool_loop_result(_valid_result(trace, ["ev_partial"]))

    assert verdict.status == "pending"
    assert verdict.evidence_complete is False
    assert verdict.evidence_error is not None
    assert verdict.evidence_error["code"] == "unconsumed_evidence"
    assert "Malformed tool-loop validation result" not in (verdict.feedback or "")


def test_submitted_typed_verdict_is_authoritative_over_malformed_text() -> None:
    trace = (
        _manifest_page(start=0, end=1, total=1, ref="ev_manifest"),
        _page(start=0, end=2_546, total=2_546, ref="ev_diff"),
    )
    result = replace(
        _valid_result(trace, ["ev_diff"]),
        text='{"status":"valid"}{"status":"invalid"}',
    )
    submitted = {
        "status": "valid",
        "feedback": "Grounded.",
        "blocking_reasons": [],
        "current_failure_evidence": [],
        "evidence_refs": ["ev_diff"],
        "evidence_complete": True,
    }

    verdict = normalize_tool_loop_result(
        result, submitted_verdict=submitted, require_submission=True
    )

    assert verdict.status == "valid"
    assert verdict.evidence_error is None


@pytest.mark.asyncio
async def test_builtin_rejects_whole_file_after_complete_aggregate_diff() -> None:
    specs = {
        spec.name: spec
        for spec in build_validation_builtins(
            task_id="task-1",
            repo_path="/repo",
            canonical_commits=(COMMIT,),
            preview_bytes=16_384,
        )
    }
    context = BuiltinExecutionContext(
        max_payload_bytes=16_000,
        evidence_ref="ev_runtime",
        subprocess_deadline=None,
    )
    page = {
        "content": {"encoding": "utf-8", "text": "diff"},
        "byte_start": 0,
        "byte_end": 4,
        "total_bytes": 4,
        "complete": True,
        "snapshot_hash": "snapshot",
        "view_hash": "view",
    }
    with (
        patch("gobby.tasks.validation_tool_loop.get_task_diff_page", return_value=page),
        patch("gobby.tasks.validation_tool_loop.read_file_at_commit") as read_file,
    ):
        diff_result = await specs["read_task_diff"].handler({}, context)
        file_result = await specs["read_file_at_commit"].handler(
            {"commit": COMMIT, "path_selector": "opaque"}, context
        )

    assert diff_result.complete is True
    assert file_result.error_code == "aggregate_diff_complete"
    read_file.assert_not_called()


@pytest.mark.asyncio
async def test_verdict_submission_builtin_is_single_assignment() -> None:
    sink = ValidationVerdictSink()
    specs = {
        spec.name: spec
        for spec in build_validation_builtins(
            task_id="task-1",
            repo_path="/repo",
            canonical_commits=(COMMIT,),
            preview_bytes=16_384,
            verdict_sink=sink,
        )
    }
    issued_ref = "1ed14cbd80f6c42cbf130e61f5e26b43"
    typo_ref = "1ed14cbd80f6c42cb130e61f5e26b43"
    evidence_context = BuiltinExecutionContext(
        max_payload_bytes=16_000,
        evidence_ref=issued_ref,
        subprocess_deadline=None,
    )
    submit_context = replace(evidence_context, evidence_ref="ev_submit")
    page = {
        "content": {"encoding": "utf-8", "text": "diff"},
        "byte_start": 0,
        "byte_end": 4,
        "total_bytes": 4,
        "complete": True,
        "snapshot_hash": "snapshot",
        "view_hash": "view",
    }
    payload = {
        "status": "valid",
        "feedback": "Grounded.",
        "blocking_reasons": [],
        "current_failure_evidence": [],
        "evidence_refs": [issued_ref],
        "evidence_complete": True,
    }

    with patch("gobby.tasks.validation_tool_loop.get_task_diff_page", return_value=page):
        evidence = await specs["read_task_diff"].handler({}, evidence_context)
    invalid = await specs["submit_validation_verdict"].handler(
        {**payload, "evidence_refs": [typo_ref]}, submit_context
    )
    corrected = await specs["submit_validation_verdict"].handler(payload, submit_context)
    duplicate = await specs["submit_validation_verdict"].handler(payload, submit_context)

    assert evidence.ok is True
    assert invalid.error_code == "invalid_evidence_reference"
    assert invalid.details == {
        "evidence_refs": [typo_ref],
        "issued_evidence_refs": [issued_ref],
    }
    assert corrected.ok is True
    assert sink.payload == payload
    assert duplicate.error_code == "verdict_already_submitted"
