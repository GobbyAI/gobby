"""Focused contracts for tool-loop task validation."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.ai import (
    AICapability,
    BuiltinExecutionContext,
    CapabilityUnavailableError,
    ToolChatRequest,
    ToolChatResult,
)
from gobby.config.tasks import TaskValidationConfig
from gobby.tasks.diff_paging import DiffPagingError, ManifestItem
from gobby.tasks.validation import TaskValidator, ValidationResult
from gobby.tasks.validation_tool_loop import (
    ValidationVerdictSink,
    build_validation_builtins,
    is_doc_only_manifest,
    normalize_tool_loop_result,
    prepare_validation_diff,
    validate_with_tool_loop,
)

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
COMMIT_C = "c" * 40
COMMIT_D = "d" * 40


def _commit_page(*items: str, total: int | None = None) -> dict[str, object]:
    page_total = len(items) if total is None else total
    return {
        "items": list(items),
        "cursor_offset": 0,
        "cursor_limit": len(items),
        "cursor_end": len(items),
        "total": page_total,
        "complete": len(items) == page_total,
    }


def _trace(evidence_ref: str) -> tuple[dict[str, object], ...]:
    return (
        {
            "tool_name": "list_changed_files",
            "arguments": {"offset": 0},
            "result_size_bytes": 64,
            "ok": True,
            "error_code": None,
            "evidence_ref": "ev_manifest",
            "selector": {"kind": "changed_files"},
            "range": {"cursor_offset": 0, "cursor_end": 1, "total": 1},
            "complete": True,
            "content_hash": "snapshot-hash",
        },
        {
            "tool_name": "read_task_diff",
            "arguments": {"offset_bytes": 0},
            "result_size_bytes": 512,
            "ok": True,
            "error_code": None,
            "evidence_ref": evidence_ref,
            "selector": {"kind": "task_diff"},
            "range": {"byte_start": 0, "byte_end": 128, "total_bytes": 128},
            "complete": True,
            "content_hash": "snapshot-hash",
        },
    )


def _tool_result(
    *,
    payload: dict[str, object],
    evidence_ref: str = "ev_runtime",
    budget_exhausted: bool = False,
) -> ToolChatResult:
    return ToolChatResult(
        text=json.dumps(payload),
        trace=_trace(evidence_ref),
        calls_used=len(_trace(evidence_ref)),
        budget_exhausted=budget_exhausted,
        trace_available=True,
    )


def _validator(
    tool_result: ToolChatResult | Exception,
    *,
    config: TaskValidationConfig | None = None,
) -> tuple[TaskValidator, AsyncMock, AsyncMock]:
    llm_service = AsyncMock()
    tool_chat_service = AsyncMock()
    if isinstance(tool_result, Exception):
        tool_chat_service.chat_result.side_effect = tool_result
    else:

        async def submit_verdict(request: ToolChatRequest) -> ToolChatResult:
            specs = {spec.name: spec for spec in request.builtins}
            evidence_page = {
                "manifest": {
                    "items": [{"path_selector": "opaque"}],
                    "cursor_offset": 0,
                    "cursor_end": 1,
                    "total": 1,
                    "complete": True,
                },
                "content": {"encoding": "utf-8", "text": "diff"},
                "byte_start": 0,
                "byte_end": 4,
                "total_bytes": 4,
                "complete": True,
                "snapshot_hash": "snapshot-hash",
                "view_hash": "view-hash",
            }
            with patch(
                "gobby.tasks.validation_tool_loop.get_task_diff_page",
                return_value=evidence_page,
            ):
                await specs["list_changed_files"].handler(
                    {"offset": 0},
                    BuiltinExecutionContext(
                        max_payload_bytes=16_000,
                        evidence_ref="ev_manifest",
                        subprocess_deadline=None,
                    ),
                )
                await specs["read_task_diff"].handler(
                    {"offset_bytes": 0},
                    BuiltinExecutionContext(
                        max_payload_bytes=16_000,
                        evidence_ref="ev_runtime",
                        subprocess_deadline=None,
                    ),
                )
            payload = json.loads(tool_result.text)
            if isinstance(payload, dict):
                await specs["submit_validation_verdict"].handler(
                    payload,
                    BuiltinExecutionContext(
                        max_payload_bytes=16_000,
                        evidence_ref="ev_submit",
                        subprocess_deadline=None,
                    ),
                )
            return tool_result

        tool_chat_service.chat_result.side_effect = submit_verdict
    validator = TaskValidator(
        config or TaskValidationConfig(),
        llm_service,
        tool_chat_service=tool_chat_service,
    )
    return validator, llm_service, tool_chat_service


async def _validate_linked(
    validator: TaskValidator,
    *,
    linked_commits: tuple[str, ...] = (COMMIT_A, COMMIT_B),
    first_commits_page: dict[str, object] | None = None,
    static_evidence_loader: MagicMock | None = None,
    manifest_items: tuple[ManifestItem, ...] = (),
    manifest_count: int = 3,
    diff_total_bytes: int = 1_566,
    verification_items: tuple[dict[str, object], ...] = (),
) -> ValidationResult:
    return await validator.validate_task(
        task_id="task-1",
        title="Ground task validation",
        description=None,
        changes_summary="Implemented the requested validator.",
        validation_criteria="Focused tests pass.",
        category="code",
        verification_evidence="pytest: 12 passed",
        repo_path="/repo",
        linked_commits=linked_commits,
        first_commits_page=first_commits_page or _commit_page(COMMIT_A, total=2),
        manifest_items=manifest_items,
        manifest_count=manifest_count,
        diff_total_bytes=diff_total_bytes,
        verification_items=verification_items,
        static_evidence_loader=static_evidence_loader,
    )


@pytest.mark.asyncio
async def test_tool_loop_keeps_static_diff_evidence_lazy() -> None:
    validator, llm_service, _ = _validator(
        _tool_result(
            payload={
                "status": "valid",
                "feedback": "All criteria are verified.",
                "blocking_reasons": [],
                "current_failure_evidence": [],
                "evidence_refs": ["ev_runtime"],
                "evidence_complete": True,
            }
        )
    )
    static_loader = MagicMock(side_effect=AssertionError("static evidence loaded eagerly"))

    result = await _validate_linked(validator, static_evidence_loader=static_loader)

    assert result.status == "valid"
    assert result.mode == "tool_loop"
    assert result.evidence_refs == ("ev_runtime",)
    assert result.evidence_complete is True
    static_loader.assert_not_called()
    llm_service.call_json_feature.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_linked_commits_route_to_static_validation() -> None:
    validator, llm_service, tool_chat_service = _validator(
        _tool_result(payload={"status": "valid"})
    )
    llm_service.call_json_feature.return_value = {
        "status": "valid",
        "feedback": "Static evidence passes.",
    }
    static_loader = MagicMock(return_value=("Static diff evidence", None))

    result = await validator.validate_task(
        task_id="task-1",
        title="Summary-only task",
        description="Validate the supplied summary.",
        changes_summary="Summary",
        linked_commits=(),
        static_evidence_loader=static_loader,
    )

    assert result.status == "valid"
    assert result.mode == "static"
    assert result.evidence_complete is True
    static_loader.assert_called_once_with()
    tool_chat_service.chat_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_capability_unavailable_falls_back_to_static_validation() -> None:
    validator, llm_service, _ = _validator(
        CapabilityUnavailableError(AICapability.TOOL_CHAT, reason="no traced adapter")
    )
    llm_service.call_json_feature.return_value = {
        "status": "valid",
        "feedback": "Static fallback passes.",
    }
    static_loader = MagicMock(return_value=("Static linked diff", "Referenced file"))

    result = await _validate_linked(validator, static_evidence_loader=static_loader)

    assert result.status == "valid"
    assert result.mode == "static"
    static_loader.assert_called_once_with()


@pytest.mark.asyncio
async def test_tool_loop_missing_evidence_fields_is_pending() -> None:
    validator, _, _ = _validator(
        _tool_result(
            payload={
                "status": "valid",
                "feedback": "Claims success without grounded fields.",
                "blocking_reasons": [],
                "current_failure_evidence": [],
            }
        )
    )

    result = await _validate_linked(validator)

    assert result.status == "pending"
    assert result.mode == "tool_loop"
    assert result.evidence_error == {
        "code": "verdict_protocol_error",
        "message": "evidence_refs and evidence_complete are required",
    }


@pytest.mark.asyncio
async def test_fabricated_ref_is_rejected_before_terminal_verdict() -> None:
    validator, _, _ = _validator(
        _tool_result(
            payload={
                "status": "valid",
                "feedback": "Claims fabricated evidence.",
                "blocking_reasons": [],
                "current_failure_evidence": [],
                "evidence_refs": ["ev_fabricated"],
                "evidence_complete": True,
            }
        )
    )

    result = await _validate_linked(validator)

    assert result.status == "pending"
    assert result.evidence_refs == ("ev_manifest", "ev_runtime")
    assert "submit_validation_verdict was not called" in (result.feedback or "")


@pytest.mark.asyncio
async def test_verified_refs_and_trace_are_in_feedback() -> None:
    validator, _, _ = _validator(
        _tool_result(
            payload={
                "status": "valid",
                "feedback": "Grounded verdict.",
                "blocking_reasons": [],
                "current_failure_evidence": [],
                "evidence_refs": ["ev_runtime"],
                "evidence_complete": True,
            }
        )
    )

    result = await _validate_linked(validator)

    assert result.status == "valid"
    assert '"mode":"tool_loop"' in (result.feedback or "")
    assert '"evidence_refs":["ev_runtime"]' in (result.feedback or "")
    assert '"tool_name":"read_task_diff"' in (result.feedback or "")
    assert "snapshot-hash" in (result.feedback or "")


@pytest.mark.asyncio
async def test_complete_evidence_verdict_survives_adapter_budget_flag() -> None:
    sink = ValidationVerdictSink()
    sink.record_evidence_ref("ev_runtime")
    submission = sink.submit(
        {
            "status": "valid",
            "feedback": "Grounded verdict.",
            "blocking_reasons": [],
            "current_failure_evidence": [],
            "evidence_refs": ["ev_runtime"],
            "evidence_complete": True,
        }
    )
    result = normalize_tool_loop_result(
        ToolChatResult(
            text="",
            trace=_trace("ev_runtime"),
            calls_used=2,
            budget_exhausted=True,
            trace_available=True,
            stop_reason="max_turns",
        ),
        submitted_verdict=sink.payload,
        require_submission=True,
    )

    assert submission.ok is True
    assert result.status == "valid"
    assert result.evidence_complete is True


@pytest.mark.asyncio
async def test_prompt_uses_commit_count_first_page_and_cursor_metadata() -> None:
    validator, _, tool_chat_service = _validator(
        _tool_result(
            payload={
                "status": "valid",
                "feedback": "Grounded verdict.",
                "blocking_reasons": [],
                "current_failure_evidence": [],
                "evidence_refs": ["ev_runtime"],
                "evidence_complete": True,
            }
        )
    )
    first_page = _commit_page(COMMIT_A, COMMIT_B, total=4)

    await _validate_linked(
        validator,
        linked_commits=(COMMIT_A, COMMIT_B, COMMIT_C, COMMIT_D),
        first_commits_page=first_page,
    )

    request = tool_chat_service.chat_result.await_args.args[0]
    assert "Linked commit count: 4" in request.prompt
    assert COMMIT_A in request.prompt
    assert COMMIT_B in request.prompt
    assert COMMIT_C not in request.prompt
    assert COMMIT_D not in request.prompt
    assert '"cursor_end":2' in request.prompt
    assert '"total":4' in request.prompt
    assert "list_linked_commits" not in request.prompt
    assert "Changed-file manifest count: 3" in request.prompt
    assert "Aggregate task-diff bytes: 1566" in request.prompt
    assert "without commit or path_selector" in request.prompt
    assert "pytest: 12 passed" not in request.prompt
    assert "No runtime-recorded command evidence items are available." in request.prompt
    assert "Implemented the requested validator." not in request.prompt
    assert "diff --git" not in request.prompt
    assert request.tool_policy.tools == ()
    assert request.builtins[-1].name == "submit_validation_verdict"
    verdict_schema = request.builtins[-1].input_schema
    assert "current_failure_evidence" in verdict_schema["required"]
    assert verdict_schema["properties"]["current_failure_evidence"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert "TDD red-phase history" in request.prompt
    assert "FAILED=1" in request.prompt
    assert "return an empty current_failure_evidence array" in request.prompt
    assert request.limits.max_tool_calls == 7
    assert request.limits.tool_timeout_seconds == 30.0


@pytest.mark.asyncio
async def test_request_wires_independent_turn_and_call_budgets() -> None:
    validator, _, tool_chat_service = _validator(
        _tool_result(
            payload={
                "status": "valid",
                "feedback": "Grounded verdict.",
                "blocking_reasons": [],
                "current_failure_evidence": [],
                "evidence_refs": ["ev_runtime"],
                "evidence_complete": True,
            }
        )
    )

    await _validate_linked(validator)

    request = tool_chat_service.chat_result.await_args.args[0]
    assert request.max_turns == 16
    assert request.limits.max_turns == 16
    assert request.max_turns == 2 * request.limits.max_tool_calls + 2
    assert request.limits.max_tool_calls == 7


@pytest.mark.asyncio
async def test_non_default_call_cap_reaches_bounded_request() -> None:
    validator, _, tool_chat_service = _validator(
        _tool_result(
            payload={
                "status": "pending",
                "feedback": "Bounded.",
                "blocking_reasons": [],
                "current_failure_evidence": [],
                "evidence_refs": ["ev_runtime"],
                "evidence_complete": False,
            }
        ),
        config=TaskValidationConfig(tool_loop_max_calls=20),
    )

    await _validate_linked(validator, diff_total_bytes=300_000)

    request = tool_chat_service.chat_result.await_args.args[0]
    assert request.limits.max_tool_calls == 20
    assert request.max_turns == 42
    assert "at most 14 content calls" in request.prompt


def test_doc_only_classification_uses_complete_manifest_paths() -> None:
    manifest = (
        {
            "commit": COMMIT_A,
            "status": "M",
            "path": {"encoding": "utf-8", "text": "docs/guide.md"},
            "path_selector": "selector-1",
        },
        {
            "commit": COMMIT_A,
            "status": "M",
            "path": {"encoding": "utf-8", "text": "README.rst"},
            "path_selector": "selector-2",
        },
    )

    assert is_doc_only_manifest(manifest) is True
    assert (
        is_doc_only_manifest(
            (*manifest, {**manifest[0], "path": {"encoding": "utf-8", "text": "src/app.py"}})
        )
        is False
    )


def test_prepare_validation_diff_pages_manifest_without_raw_diff() -> None:
    first = {
        "content": {"encoding": "utf-8", "text": "ignored diff bytes"},
        "byte_start": 0,
        "byte_end": 4,
        "total_bytes": 100,
        "complete": False,
        "commits": {
            **_commit_page(COMMIT_A, total=2),
            "cursor_limit": 1,
        },
        "manifest": {
            "items": [
                {
                    "commit": COMMIT_A,
                    "status": "M",
                    "path": {"encoding": "utf-8", "text": "docs/one.md"},
                    "path_selector": "one",
                }
            ],
            "cursor_offset": 0,
            "cursor_limit": 1,
            "cursor_end": 1,
            "total": 2,
            "complete": False,
        },
        "snapshot_hash": "snapshot",
        "view_hash": "view",
    }
    second = {
        **first,
        "commits": {
            "items": [COMMIT_B],
            "cursor_offset": 1,
            "cursor_limit": 1,
            "cursor_end": 2,
            "total": 2,
            "complete": True,
        },
        "manifest": {
            "items": [
                {
                    "commit": COMMIT_B,
                    "status": "M",
                    "path": {"encoding": "utf-8", "text": "docs/two.md"},
                    "path_selector": "two",
                }
            ],
            "cursor_offset": 1,
            "cursor_limit": 1,
            "cursor_end": 2,
            "total": 2,
            "complete": True,
        },
    }

    with patch(
        "gobby.tasks.validation_tool_loop.get_task_diff_page",
        side_effect=[first, second],
    ) as get_page:
        prepared = prepare_validation_diff(
            "task-1",
            MagicMock(),
            repo_path="/repo",
            commits_page_limit=1,
            manifest_page_limit=1,
        )

    assert prepared.canonical_commits == (COMMIT_A, COMMIT_B)
    assert len(prepared.manifest_items) == 2
    assert prepared.manifest_count == 2
    assert prepared.diff_total_bytes == 100
    assert is_doc_only_manifest(prepared.manifest_items) is True
    assert get_page.call_count == 2
    assert all(call.kwargs["limit_bytes"] == 4 for call in get_page.call_args_list)


@pytest.mark.asyncio
async def test_builtin_commit_is_checked_against_canonical_closure() -> None:
    builtins = build_validation_builtins(
        task_id="task-1",
        repo_path="/repo",
        canonical_commits=(COMMIT_A,),
        preview_bytes=16_384,
    )
    read_diff = next(spec for spec in builtins if spec.name == "read_task_diff")
    context = BuiltinExecutionContext(
        max_payload_bytes=16_000,
        evidence_ref="ev_runtime",
        subprocess_deadline=None,
    )

    with pytest.raises(DiffPagingError) as exc_info:
        await read_diff.handler({"commit": COMMIT_B}, context)

    assert exc_info.value.code == "commit_not_linked"


@pytest.mark.asyncio
async def test_bounded_builtins_forbid_broad_views_but_allow_per_file() -> None:
    builtins = build_validation_builtins(
        task_id="task-1",
        repo_path="/repo",
        canonical_commits=(COMMIT_A,),
        preview_bytes=16_384,
        bounded_mode=True,
    )
    read_diff = next(spec for spec in builtins if spec.name == "read_task_diff")
    context = BuiltinExecutionContext(
        max_payload_bytes=16_000,
        evidence_ref="ev_file",
        subprocess_deadline=None,
    )

    aggregate = await read_diff.handler({}, context)
    per_commit = await read_diff.handler({"commit": COMMIT_A}, context)
    page = {
        "content": {"encoding": "utf-8", "text": "diff"},
        "byte_start": 0,
        "byte_end": 4,
        "total_bytes": 4,
        "complete": True,
        "snapshot_hash": "snapshot-hash",
        "view_hash": "view-hash",
    }
    with patch("gobby.tasks.validation_tool_loop.get_task_diff_page", return_value=page):
        per_file = await read_diff.handler({"commit": COMMIT_A, "path_selector": "opaque"}, context)

    assert aggregate.error_code == "bounded_view_forbidden"
    assert per_commit.error_code == "bounded_view_forbidden"
    assert per_file.ok is True
    assert per_file.selector == {
        "kind": "task_diff",
        "task_id": "task-1",
        "commit": COMMIT_A,
        "path_selector": "opaque",
    }


def test_bounded_sink_rejects_complete_claim_then_accepts_correction() -> None:
    sink = ValidationVerdictSink(bounded_mode=True)
    sink.record_evidence_ref("ev_file")
    payload = {
        "status": "valid",
        "feedback": "Grounded verdict.",
        "blocking_reasons": [],
        "current_failure_evidence": [],
        "evidence_refs": ["ev_file"],
        "evidence_complete": True,
    }

    rejected = sink.submit(payload)
    payload["evidence_complete"] = False
    accepted = sink.submit(payload)

    assert rejected.error_code == "evidence_complete_invalid"
    assert accepted.ok is True
    assert sink.payload == payload


@pytest.mark.asyncio
async def test_verification_evidence_builtin_pages_successes_and_failures() -> None:
    sink = ValidationVerdictSink()
    builtins = build_validation_builtins(
        task_id="task-1",
        repo_path="/repo",
        canonical_commits=(COMMIT_A,),
        preview_bytes=16_384,
        verdict_sink=sink,
        verification_items=(
            {"success": True, "command": "pytest tests/a.py", "private": "discard"},
            {"success": False, "command": "ruff check", "exit_code": 1},
        ),
    )
    list_evidence = next(spec for spec in builtins if spec.name == "list_verification_evidence")
    context = BuiltinExecutionContext(
        max_payload_bytes=16_000,
        evidence_ref="ev_commands",
        subprocess_deadline=None,
    )

    first = await list_evidence.handler({"offset": 0, "limit": 1}, context)
    second = await list_evidence.handler({"offset": 1, "limit": 1}, context)
    out_of_range = await list_evidence.handler({"offset": 3, "limit": 1}, context)

    assert first.payload == {
        "items": [{"success": True, "command": "pytest tests/a.py"}],
        "total": 2,
    }
    assert first.complete is False
    assert second.payload == {
        "items": [{"success": False, "command": "ruff check", "exit_code": 1}],
        "total": 2,
    }
    assert second.complete is True
    assert out_of_range.error_code == "cursor_out_of_range"
    assert sink.issued_evidence_refs == {"ev_commands"}


def test_bounded_normalization_attaches_disclosure_and_rejects_complete_claim() -> None:
    trace = (
        _trace("ev_file")[0],
        {
            **_trace("ev_file")[1],
            "arguments": {"commit": COMMIT_A, "path_selector": "opaque"},
            "selector": {
                "kind": "task_diff",
                "task_id": "task-1",
                "commit": COMMIT_A,
                "path_selector": "opaque",
            },
        },
    )
    result = ToolChatResult(text="", trace=trace, trace_available=True)
    payload = {
        "status": "valid",
        "feedback": "Selected file satisfies the criteria.",
        "blocking_reasons": [],
        "current_failure_evidence": [],
        "evidence_refs": ["ev_file"],
        "evidence_complete": False,
    }
    summary = {
        "manifest_total": 2,
        "inspected_count": 1,
        "uninspected_count": 1,
        "inspected_paths": ["src/a.py"],
        "uninspected_sample": ["src/b.py"],
        "content_call_budget": 26,
    }

    verdict = normalize_tool_loop_result(
        result,
        submitted_verdict=payload,
        require_submission=True,
        bounded_mode=True,
        inspection_summary=summary,
    )
    invalid_claim = normalize_tool_loop_result(
        result,
        submitted_verdict={**payload, "evidence_complete": True},
        require_submission=True,
        bounded_mode=True,
        inspection_summary=summary,
    )

    assert verdict.status == "valid"
    assert verdict.evidence_complete is False
    assert verdict.inspection_summary == summary
    assert verdict.feedback is not None
    provenance = json.loads(verdict.feedback.rsplit("\n", 1)[-1])
    assert provenance["bounded_inspection"] == summary
    assert invalid_claim.status == "pending"
    assert invalid_claim.evidence_error == {
        "code": "verdict_protocol_error",
        "message": "bounded verdicts must submit evidence_complete=false",
    }


@pytest.mark.asyncio
async def test_oversized_diff_runs_bounded_validation_with_disclosure() -> None:
    service = AsyncMock()
    manifest_items = (
        ManifestItem(
            commit=COMMIT_A,
            status="M",
            path={"encoding": "utf-8", "text": "src/a.py"},
            path_selector="opaque-a",
            lines_added=100,
            lines_deleted=20,
        ),
        ManifestItem(
            commit=COMMIT_A,
            status="M",
            path={"encoding": "utf-8", "text": "src/b.py"},
            path_selector="opaque-b",
            lines_added=2,
            lines_deleted=1,
        ),
    )

    async def run_bounded(request: ToolChatRequest) -> ToolChatResult:
        specs = {spec.name: spec for spec in request.builtins}
        page = {
            "manifest": {
                "items": list(manifest_items),
                "cursor_offset": 0,
                "cursor_end": 2,
                "total": 2,
                "complete": True,
            },
            "content": {"encoding": "utf-8", "text": "diff"},
            "byte_start": 0,
            "byte_end": 4,
            "total_bytes": 4,
            "complete": True,
            "snapshot_hash": "snapshot-hash",
            "view_hash": "view-hash",
        }
        with patch("gobby.tasks.validation_tool_loop.get_task_diff_page", return_value=page):
            await specs["list_changed_files"].handler(
                {"offset": 0},
                BuiltinExecutionContext(
                    max_payload_bytes=16_000,
                    evidence_ref="ev_manifest",
                    subprocess_deadline=None,
                ),
            )
            await specs["read_task_diff"].handler(
                {"commit": COMMIT_A, "path_selector": "opaque-a"},
                BuiltinExecutionContext(
                    max_payload_bytes=16_000,
                    evidence_ref="ev_file",
                    subprocess_deadline=None,
                ),
            )
        await specs["submit_validation_verdict"].handler(
            {
                "status": "valid",
                "feedback": "Relevant implementation file is correct.",
                "blocking_reasons": [],
                "current_failure_evidence": [],
                "evidence_refs": ["ev_file"],
                "evidence_complete": False,
            },
            BuiltinExecutionContext(
                max_payload_bytes=16_000,
                evidence_ref="ev_submit",
                subprocess_deadline=None,
            ),
        )
        trace = (
            {
                **_trace("ev_file")[0],
                "range": {"cursor_offset": 0, "cursor_end": 2, "total": 2},
            },
            {
                **_trace("ev_file")[1],
                "arguments": {"commit": COMMIT_A, "path_selector": "opaque-a"},
                "selector": {
                    "kind": "task_diff",
                    "task_id": "task-1",
                    "commit": COMMIT_A,
                    "path_selector": "opaque-a",
                },
            },
        )
        return ToolChatResult(text="", trace=trace, calls_used=3, trace_available=True)

    service.chat_result.side_effect = run_bounded
    verdict = await validate_with_tool_loop(
        service,
        TaskValidationConfig(),
        task_id="task-1",
        title="Validate a large change",
        description="Inspect the relevant files.",
        validation_criteria="Implementation is correct.",
        category="code",
        repo_path="/repo",
        canonical_commits=(COMMIT_A,),
        first_commits_page=_commit_page(COMMIT_A),
        manifest_items=manifest_items,
        manifest_count=2,
        diff_total_bytes=1_078_130,
    )

    request = service.chat_result.call_args.args[0]
    assert request.limits.max_tool_calls == 32
    assert request.max_turns == request.limits.max_turns == 66
    assert request.candidate_timeout_seconds == 180.0
    assert request.cli_candidate_timeout_seconds == 180.0
    assert "at most 26 content calls" in request.prompt
    assert "evidence_complete=false" in request.prompt
    assert verdict.status == "valid"
    assert verdict.evidence_complete is False
    assert verdict.inspection_summary == {
        "manifest_total": 2,
        "inspected_count": 1,
        "uninspected_count": 1,
        "inspected_paths": ["src/a.py"],
        "uninspected_sample": ["src/b.py"],
        "content_call_budget": 26,
    }


@pytest.mark.asyncio
async def test_validation_request_uses_configured_full_candidate_timeout() -> None:
    service = AsyncMock()
    service.chat_result.return_value = ToolChatResult(
        text="",
        trace=(),
        calls_used=0,
        trace_available=False,
    )
    config = TaskValidationConfig(cli_candidate_timeout_seconds=12.5)

    await validate_with_tool_loop(
        service,
        config,
        task_id="task-1",
        title="Validate timeout propagation",
        description=None,
        validation_criteria="The candidate loop is bounded.",
        category="code",
        repo_path="/repo",
        canonical_commits=(COMMIT_A,),
        first_commits_page=_commit_page(COMMIT_A),
        manifest_items=(),
        manifest_count=0,
        diff_total_bytes=1,
    )

    request = service.chat_result.call_args.args[0]
    assert request.candidate_timeout_seconds == 12.5
    assert request.cli_candidate_timeout_seconds == 12.5


@pytest.mark.asyncio
async def test_infeasible_manifest_returns_typed_error_without_model_call() -> None:
    service = AsyncMock()

    verdict = await validate_with_tool_loop(
        service,
        TaskValidationConfig(),
        task_id="task-1",
        title="Validate an infeasible change",
        description=None,
        validation_criteria="Implementation is correct.",
        category="code",
        repo_path="/repo",
        canonical_commits=(COMMIT_A,),
        first_commits_page=_commit_page(COMMIT_A),
        manifest_items=(),
        manifest_count=6_000,
        diff_total_bytes=1,
    )

    service.chat_result.assert_not_awaited()
    assert verdict.status == "pending"
    assert verdict.evidence_error == {
        "code": "evidence_budget_exceeded",
        "required_tool_calls": 36,
        "configured_max_calls": 32,
        "content_call_budget": 0,
        "min_content_calls": 8,
        "manifest_pages": 30,
        "artifacts": [
            {
                "selector": {"kind": "task_diff", "task_id": "task-1"},
                "total_bytes": 1,
                "unconsumed_ranges": [[0, 1]],
            }
        ],
    }


def test_tool_loop_config_defaults() -> None:
    config = TaskValidationConfig()

    assert config.tool_loop_enabled is True
    assert config.tool_loop_preview_bytes == 16_384


def _submitted_payload(
    *,
    status: str = "valid",
    current_failure_evidence: list[object] | None = None,
    blocking_reasons: list[str] | None = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "feedback": "Grounded verdict.",
        "blocking_reasons": blocking_reasons or [],
        "current_failure_evidence": current_failure_evidence or [],
        "evidence_refs": evidence_refs or ["ev_runtime"],
        "evidence_complete": True,
    }


def test_first_contradiction_is_rejected_then_corrected_valid_wins() -> None:
    sink = ValidationVerdictSink()
    sink.record_evidence_ref("ev_runtime")
    contradictory = _submitted_payload(current_failure_evidence=["pytest failed"])
    corrected = _submitted_payload()

    rejected = sink.submit(contradictory)
    accepted = sink.submit(corrected)
    verdict = normalize_tool_loop_result(
        _tool_result(payload=corrected),
        submitted_verdict=sink.payload,
        rejected_contradiction=sink.last_contradiction,
        require_submission=True,
    )

    assert rejected.error_code == "verdict_contradiction"
    assert "Either return status='invalid'" in (rejected.error or "")
    assert accepted.ok is True
    assert verdict.status == "valid"
    assert verdict.verdict_override is None


def test_corrected_invalid_submission_is_accepted_without_override() -> None:
    sink = ValidationVerdictSink()
    sink.record_evidence_ref("ev_runtime")
    contradictory = _submitted_payload(current_failure_evidence=["pytest failed"])
    corrected = _submitted_payload(
        status="invalid",
        current_failure_evidence=["pytest failed"],
        blocking_reasons=["pytest failed"],
    )

    sink.submit(contradictory)
    accepted = sink.submit(corrected)
    verdict = normalize_tool_loop_result(
        _tool_result(payload=corrected),
        submitted_verdict=sink.payload,
        rejected_contradiction=sink.last_contradiction,
        require_submission=True,
    )

    assert accepted.ok is True
    assert verdict.status == "invalid"
    assert verdict.blocking_reasons == ("pytest failed",)
    assert verdict.verdict_override is None


def test_repeated_contradiction_is_accepted_then_demoted() -> None:
    sink = ValidationVerdictSink()
    sink.record_evidence_ref("ev_runtime")
    contradictory = _submitted_payload(current_failure_evidence=["pytest failed"])

    first = sink.submit(contradictory)
    second = sink.submit(contradictory)
    verdict = normalize_tool_loop_result(
        _tool_result(payload=contradictory),
        submitted_verdict=sink.payload,
        rejected_contradiction=sink.last_contradiction,
        require_submission=True,
    )

    assert first.error_code == "verdict_contradiction"
    assert second.ok is True
    assert verdict.status == "invalid"
    assert verdict.blocking_reasons == ("pytest failed",)
    assert verdict.verdict_override == {
        "from": "valid",
        "to": "invalid",
        "reason": "current_failure_evidence",
        "evidence": ["pytest failed"],
    }


def test_loop_end_after_rejected_contradiction_demotes_instead_of_pending() -> None:
    sink = ValidationVerdictSink()
    sink.record_evidence_ref("ev_runtime")
    contradictory = _submitted_payload(current_failure_evidence=["pytest failed"])

    sink.submit(contradictory)
    verdict = normalize_tool_loop_result(
        _tool_result(payload=contradictory),
        submitted_verdict=None,
        rejected_contradiction=sink.last_contradiction,
        require_submission=True,
    )

    assert verdict.status == "invalid"
    assert verdict.verdict_override is not None


def test_malformed_failure_evidence_precedes_reference_and_contradiction_checks() -> None:
    payload = _submitted_payload(
        current_failure_evidence=["pytest failed", 1],
        evidence_refs=["fabricated"],
    )

    verdict = normalize_tool_loop_result(
        _tool_result(payload=payload),
        submitted_verdict=payload,
        require_submission=True,
    )

    assert verdict.status == "pending"
    assert verdict.evidence_error == {
        "code": "verdict_protocol_error",
        "message": "current_failure_evidence must be an array of strings",
    }


@pytest.mark.asyncio
async def test_tool_loop_missing_current_failure_evidence_is_pending() -> None:
    validator, _, _ = _validator(
        _tool_result(
            payload={
                "status": "valid",
                "feedback": "Grounded verdict.",
                "blocking_reasons": [],
                "evidence_refs": ["ev_runtime"],
                "evidence_complete": True,
            }
        )
    )

    result = await _validate_linked(validator)

    assert result.status == "pending"
    assert result.evidence_error == {
        "code": "verdict_protocol_error",
        "message": "current_failure_evidence is required",
    }


@pytest.mark.asyncio
async def test_tool_loop_override_propagates_into_validation_result() -> None:
    validator, _, _ = _validator(
        _tool_result(
            payload=_submitted_payload(
                current_failure_evidence=["pytest: 1 failed"],
            )
        )
    )

    result = await _validate_linked(validator)

    assert result.status == "invalid"
    assert result.blocking_reasons == ["pytest: 1 failed"]
    assert result.verdict_override == {
        "from": "valid",
        "to": "invalid",
        "reason": "current_failure_evidence",
        "evidence": ["pytest: 1 failed"],
    }
