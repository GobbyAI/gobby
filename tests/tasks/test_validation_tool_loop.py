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
from gobby.tasks.diff_paging import DiffPagingError
from gobby.tasks.validation import TaskValidator, ValidationResult
from gobby.tasks.validation_tool_loop import (
    build_validation_builtins,
    is_doc_only_manifest,
    prepare_validation_diff,
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
            submit = next(
                spec for spec in request.builtins if spec.name == "submit_validation_verdict"
            )
            payload = json.loads(tool_result.text)
            if isinstance(payload, dict):
                await submit.handler(
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
        manifest_count=3,
        diff_total_bytes=1_566,
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
async def test_fabricated_ref_is_dropped_and_flags_verdict() -> None:
    validator, _, _ = _validator(
        _tool_result(
            payload={
                "status": "valid",
                "feedback": "Claims fabricated evidence.",
                "blocking_reasons": [],
                "evidence_refs": ["ev_fabricated"],
                "evidence_complete": True,
            }
        )
    )

    result = await _validate_linked(validator)

    assert result.status == "pending"
    assert result.evidence_refs == ()
    assert "ev_fabricated" in (result.feedback or "")
    assert "runtime-issued" in (result.feedback or "")


@pytest.mark.asyncio
async def test_verified_refs_and_trace_are_in_feedback() -> None:
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

    assert result.status == "valid"
    assert '"mode":"tool_loop"' in (result.feedback or "")
    assert '"evidence_refs":["ev_runtime"]' in (result.feedback or "")
    assert '"tool_name":"read_task_diff"' in (result.feedback or "")
    assert "snapshot-hash" in (result.feedback or "")


@pytest.mark.asyncio
async def test_complete_evidence_verdict_survives_adapter_budget_flag() -> None:
    validator, _, _ = _validator(
        _tool_result(
            payload={
                "status": "valid",
                "feedback": "Incomplete investigation.",
                "blocking_reasons": [],
                "evidence_refs": ["ev_runtime"],
                "evidence_complete": True,
            },
            budget_exhausted=True,
        )
    )

    result = await _validate_linked(validator)

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
    assert "pytest: 12 passed" in request.prompt
    assert "Implemented the requested validator." not in request.prompt
    assert "diff --git" not in request.prompt
    assert request.max_turns == 7
    assert request.limits.max_turns == 7
    assert request.tool_policy.tools == ()
    assert request.builtins[-1].name == "submit_validation_verdict"
    assert request.limits.max_tool_calls == 7
    assert request.limits.tool_timeout_seconds == 30.0


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


def test_tool_loop_config_defaults() -> None:
    config = TaskValidationConfig()

    assert config.tool_loop_enabled is True
    assert config.tool_loop_preview_bytes == 16_384
