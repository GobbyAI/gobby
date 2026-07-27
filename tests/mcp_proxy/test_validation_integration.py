import copy
import hashlib
import json
import re
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.failure_categories import FailureCategory
from gobby.llm import LLMService
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._verification_evidence_context import (
    format_verification_evidence_context,
)
from gobby.storage.tasks import LocalTaskManager, StageState, Task
from gobby.storage.verification_receipts import (
    VerificationOutcome,
    VerificationReceipt,
    VerificationReceiptWrite,
    verification_receipt_id,
)
from gobby.tasks.validation import TaskValidator
from gobby.tasks.validation_verdict import ValidationResult
from gobby.utils.datetime import utc_now
from gobby.utils.session_context import session_context_for_test


def _diff_result(
    *, diff: str, commits: list[str], file_count: int, has_uncommitted_changes: bool = False
) -> tuple[str, dict[str, dict[str, int]]]:
    del has_uncommitted_changes
    return diff, {
        "commits": {"total": len(commits)},
        "manifest": {"total": file_count},
    }


def _verification_receipt(
    command: str,
    *,
    index: int = 1,
    normalized_outcome: VerificationOutcome = "success",
    exit_code: int | None = 0,
    execution_id: str | None = None,
    outcome_provenance: str = "tool_output.json.exit_code",
    output: str = "passed",
) -> VerificationReceipt:
    timestamp = utc_now() + timedelta(seconds=index)
    return VerificationReceipt(
        id=f"receipt-{index:03d}",
        project_id="p1",
        session_id="sess-uuid",
        task_id="t1",
        provider="codex",
        execution_id=execution_id or f"execution-{index:03d}",
        source_event_id=f"event-{index:03d}",
        evidence_type="shell_command",
        command=command,
        cwd="/repo",
        normalized_outcome=normalized_outcome,
        outcome_provenance=outcome_provenance,
        exit_code=exit_code,
        started_at=timestamp,
        completed_at=timestamp,
        output_first_4k=output,
        output_last_4k=output,
        output_sha256=hashlib.sha256(output.encode()).hexdigest(),
        output_bytes=len(output),
        validation_epoch=0,
        details={},
        attribution_source="sole_claim",
        attribution_actor="sess-uuid",
        attributed_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _receipt_from_write(write: VerificationReceiptWrite) -> VerificationReceipt:
    """Materialize the receipt returned by the mocked durable store."""
    output = write.output or ""
    encoded = output.encode("utf-8")
    now = utc_now()
    return VerificationReceipt(
        id=verification_receipt_id(
            write.project_id,
            write.session_id,
            write.provider,
            write.execution_id,
        ),
        project_id=write.project_id,
        session_id=write.session_id,
        task_id=write.task_id,
        provider=write.provider,
        execution_id=write.execution_id,
        source_event_id=write.source_event_id,
        evidence_type=write.evidence_type,
        command=write.command,
        cwd=write.cwd,
        normalized_outcome=write.normalized_outcome,
        outcome_provenance=write.outcome_provenance,
        exit_code=write.exit_code,
        started_at=write.started_at,
        completed_at=write.completed_at,
        output_first_4k=output[:4096] or None,
        output_last_4k=output[-4096:] or None,
        output_sha256=hashlib.sha256(encoded).hexdigest() if encoded else None,
        output_bytes=len(encoded) if write.output is not None else None,
        validation_epoch=write.validation_epoch,
        details=dict(write.details),
        attribution_source=write.attribution_source,
        attribution_actor=write.attribution_actor,
        attributed_at=write.attributed_at,
        created_at=now,
        updated_at=now,
    )


_TEST_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)
_TEST_TIMESTAMP_TEXT = _TEST_TIMESTAMP.isoformat()
_StageState = Literal["ready", "in_progress", "needs_review", "review_approved", "done"]


def test_format_verification_evidence_context_preserves_canonical_outcomes() -> None:
    context = format_verification_evidence_context(
        [
            {
                "evidence_type": "shell_command",
                "success": True,
                "command": "uv run pytest tests/failing.py",
                "exit_code": 7,
                "outcome_provenance": "tool_output.json.exit_code",
            },
            {
                "evidence_type": "shell_command",
                "success": True,
                "command": "uv run pytest tests/tasks/test_validation.py -q",
                "exit_code": 0,
                "outcome_provenance": "tool_output.json.exit_code",
            },
            {
                "evidence_type": "shell_command",
                "success": True,
                "command": "uv run pytest tests/uncorrelated.py",
            },
            {
                "evidence_type": "shell_command",
                "success": True,
                "command": "uv run ruff check src/gobby",
                "outcome_provenance": "claude.hook:PostToolUse",
            },
            {
                "evidence_type": "shell_command",
                "success": False,
                "command": "uv run mypy src/gobby",
                "outcome_provenance": "qwen.hook:PostToolUseFailure",
            },
            {
                "evidence_type": "shell_command",
                "success": True,
                "command": "uv run pytest tests/textual.py",
                "outcome_provenance": "validation_summary.pytest",
            },
            {
                "evidence_type": "manual_diff_review",
                "success": True,
                "summary": "Verified touched source line counts are below 1000",
                "supports": "source line-count gate",
                "scope": "src/gobby/mcp_proxy/tools/tasks",
                "task_id": "#15763",
            },
        ],
        limit=7,
    )

    assert context is not None
    assert context.startswith("Structured verification results")
    results = [json.loads(line) for line in context.splitlines()[1:]]
    by_command = {item["command"]: item for item in results if "command" in item}

    contradictory = by_command["uv run pytest tests/failing.py"]
    assert contradictory["exit_code"] == 7
    assert contradictory["success"] is True

    exit_code = by_command["uv run pytest tests/tasks/test_validation.py -q"]
    assert exit_code["exit_code"] == 0
    assert exit_code["success"] is True

    provider_success = by_command["uv run ruff check src/gobby"]
    assert provider_success["success"] is True

    provider_failure = by_command["uv run mypy src/gobby"]
    assert provider_failure["success"] is False

    for command in ("uv run pytest tests/textual.py", "uv run pytest tests/uncorrelated.py"):
        assert by_command[command]["success"] is True

    assert all("command_result_correlation" not in item for item in results)
    assert all("command_result_signal" not in item for item in results)

    assert any(
        item.get("summary") == "Verified touched source line counts are below 1000"
        for item in results
    )


def test_format_verification_evidence_context_bounds_oversized_result() -> None:
    context = format_verification_evidence_context(
        [
            {
                "evidence_type": "manual_review",
                "success": True,
                "summary": '"' * 20_000,
                "supports": '"' * 20_000,
                "scope": '"' * 20_000,
                "matcher_id": '"' * 20_000,
                "matcher_label": '"' * 20_000,
                "outcome_provenance": '"' * 20_000,
                "task_id": '"' * 20_000,
            }
        ],
        limit=1,
    )

    assert context is not None
    assert len(context) <= 8_000
    assert '"evidence_type":"validation_evidence_overflow"' in context
    assert '"success":null' in context


@pytest.fixture
def repo_path(tmp_path: Path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    return str(repo)


def _stage(task_id: str, state: _StageState) -> StageState:
    return StageState(
        task_id=task_id,
        stage_name="development",
        position=0,
        state=state,
        review_policy="required",
        reviewer_agent=None,
        entered_at=_TEST_TIMESTAMP_TEXT,
        entered_by_session_id=None,
        completed_at=None,
        completed_by_session_id=None,
        completed_commit_sha=None,
        work_attempt_count=0,
        review_round_count=0,
        max_work_attempts=None,
        max_review_rounds=None,
        artifact_refs=None,
        notes=None,
        updated_at=_TEST_TIMESTAMP,
    )


def _task(**kwargs: Any) -> Task:
    status = kwargs.pop("status", "open")
    task_id = kwargs["id"]
    if status == "closed":
        kwargs.setdefault("closed_at", "now")
    elif status == "escalated":
        kwargs.setdefault("is_escalated", True)
        kwargs.setdefault("escalated_at", "now")
    elif status in {"ready", "in_progress", "needs_review", "review_approved"}:
        kwargs.setdefault("stages", (_stage(task_id, status),))
    for field_name in ("created_at", "updated_at", "closed_at", "escalated_at"):
        if kwargs.get(field_name) == "now":
            kwargs[field_name] = _TEST_TIMESTAMP
    return Task(**kwargs)


# close_task requires an active session context after Change 4 — seed one for
# every test in this module; tests that need to exercise the guard itself live
# in tests/mcp_proxy/tools/test_task_lifecycle_coverage.py.
@pytest.fixture(autouse=True)
def _seed_session_context() -> Generator[None]:
    with session_context_for_test("validation-integration-session"):
        yield


@pytest.fixture(autouse=True)
def _mock_verification_receipt_store(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    store = MagicMock()
    store.list_for_task.return_value = []
    store.count_unassigned.return_value = 0
    store.upsert.side_effect = _receipt_from_write
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._lifecycle_close.VerificationReceiptStore",
        lambda _db: store,
    )
    return store


@pytest.fixture
def mock_task_manager() -> MagicMock:
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()  # Needed for dep_manager init
    manager.db.fetchone.return_value = None
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    manager.db.transaction.return_value.__enter__.return_value = conn
    manager.increment_validation_failure.return_value = (1, False)
    return manager


@pytest.fixture
def mock_task_validator() -> AsyncMock:
    validator = AsyncMock(spec=TaskValidator)
    return validator


def _paired_codex_receipts(commands: list[str]) -> list[VerificationReceipt]:
    receipts: list[VerificationReceipt] = []
    for command_index, command in enumerate(commands):
        output = f"gate {command_index} passed\n"
        receipts.extend(
            [
                _verification_receipt(
                    command,
                    index=command_index * 2 + 1,
                    normalized_outcome="unknown",
                    exit_code=None,
                    execution_id=f"exec-{command_index}",
                    outcome_provenance="before_tool",
                    output=output,
                ),
                _verification_receipt(
                    command,
                    index=command_index * 2 + 2,
                    execution_id=f"call_{command_index}:0",
                    output=output,
                ),
            ]
        )
    return receipts


async def _preview_and_close_with_receipts(
    task_manager: MagicMock,
    task_validator: AsyncMock,
    repo_path: str,
    receipts: list[VerificationReceipt],
    *,
    expected_successes: int,
) -> tuple[dict[str, Any], list[str]]:
    task = _task(
        id="t1",
        title="Canonical receipt close",
        project_id="p1",
        status="open",
        description="Close with canonical receipts.",
        validation_criteria="Required focused commands pass.",
        commits=["a1"],
        claimed_by_session_id="sess-uuid",
        priority=1,
        task_type="task",
        category="code",
        created_at="now",
        updated_at="now",
    )
    packets: list[str] = []

    def validate_packet(*args: Any, **kwargs: Any) -> ValidationResult:
        receipt_text = kwargs["verification_receipt_text"]
        assert isinstance(receipt_text, str)
        packets.append(receipt_text)
        payload = json.loads(receipt_text.removeprefix("Verification receipt packet:\n"))
        successful_commands = sum(
            item["outcome"] == "success" and item["evidence_type"] == "shell_command"
            for item in payload["receipt_catalog"]
        )
        valid = successful_commands == expected_successes
        return ValidationResult(
            status="valid" if valid else "pending",
            feedback="Canonical receipt verdict.",
            blocking_reasons=[] if valid else ["Required command outcome remains unknown."],
            failure_category=None if valid else FailureCategory.TEST,
        )

    task_manager.get_task.return_value = task
    task_manager.list_tasks.return_value = []
    task_manager.close_task.return_value = task
    task_validator.validate_task.side_effect = validate_packet

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as mock_sm_cls,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager") as mock_stm_cls,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as mock_svm_cls,
        patch(
            "gobby.tasks.commits.collect_task_diff_text",
            return_value=_diff_result(
                diff="diff --git a/a.py b/a.py\n+canonical\n",
                commits=["a1"],
                file_count=1,
            ),
        ),
        patch("gobby.tasks.task_state_evidence.utc_now", return_value=_TEST_TIMESTAMP),
        patch("gobby.utils.git.normalize_commit_sha", side_effect=lambda sha, cwd=None: sha),
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.VerificationReceiptStore"
        ) as receipt_store_cls,
    ):
        mock_pm.return_value.get.return_value = MagicMock(repo_path=repo_path)
        mock_sm_cls.return_value.resolve_session_reference.return_value = "sess-uuid"
        mock_stm_cls.return_value.get_task_sessions.return_value = [
            {
                "session_id": "sess-uuid",
                "task_id": "t1",
                "action": "claimed",
                "created_at": "2026-05-01T00:00:00+00:00",
            }
        ]
        mock_svm_cls.return_value.get_variables.return_value = {}
        receipt_store_cls.return_value.list_for_task.return_value = receipts
        receipt_store_cls.return_value.count_unassigned.return_value = 0
        receipt_store_cls.return_value.upsert.side_effect = _receipt_from_write
        registry = create_task_registry(
            task_manager=task_manager,
            task_validator=task_validator,
        )
        close_args = {
            "task_id": "t1",
            "commit_sha": "a1",
            "changes_summary": "Implemented canonical receipt reconciliation.",
        }
        result = await registry.call("close_task", {**close_args, "preview": True})

    return result, packets


# ============================================================================
# Close Task with Commit-Based Validation Tests
# ============================================================================


async def _close_with_static_verdict(
    make_task_validator: Callable[..., TaskValidator],
    task_manager: MagicMock,
    repo_path: str,
    payload: dict[str, object],
    *,
    diff: str = "diff --git a/check.sh b/check.sh\n+FAILED=1\n",
) -> tuple[dict[str, Any], MagicMock, AsyncMock]:
    task = _task(
        id="structured-verdict-task",
        title="Structured validation verdict",
        project_id="p1",
        status="open",
        description="Implement structured validation.",
        validation_criteria="Focused tests pass.",
        commits=["abc123"],
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    task_manager.get_task.return_value = task
    task_manager.list_tasks.return_value = []
    task_manager.close_task.return_value = task
    task_manager.increment_validation_failure.return_value = (1, False)
    llm_service = MagicMock(spec=LLMService)

    async def validation_response(
        _config: Any,
        prompt: str,
        **_kwargs: Any,
    ) -> dict[str, object]:
        response = copy.deepcopy(payload)
        receipt_match = re.search(r'"receipt_id":"([^"]+)"', prompt)
        assert receipt_match is not None
        criterion_results = response.get("criterion_results")
        if isinstance(criterion_results, list):
            for criterion_result in criterion_results:
                if (
                    isinstance(criterion_result, dict)
                    and criterion_result.get("status") == "satisfied"
                ):
                    criterion_result["evidence_ids"] = [receipt_match.group(1)]
        return response

    llm_service.call_json_feature = AsyncMock(side_effect=validation_response)
    validator = make_task_validator(
        TaskValidationConfig(enabled=True),
        llm_service,
    )
    validator._loader = MagicMock()
    validator._loader.render.side_effect = lambda _path, context: context["changes_section"]

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch(
            "gobby.tasks.commits.collect_task_diff_text",
            return_value=_diff_result(diff=diff, commits=["abc123"], file_count=1),
        ),
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as project_manager,
        patch("gobby.utils.git.run_git_command", return_value="abc123"),
        patch("gobby.utils.git.normalize_commit_sha", side_effect=lambda sha, cwd=None: sha),
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_validation._record_validation_iteration"
        ) as record_iteration,
        patch(
            "gobby.ai._tool_chat_service.ToolChatService.chat_result",
            new_callable=AsyncMock,
        ) as tool_chat,
    ):
        project_manager.return_value.get.return_value = MagicMock(repo_path=repo_path)
        registry = create_task_registry(
            task_manager=task_manager,
            task_validator=validator,
        )
        result = await registry.call(
            "close_task",
            {"task_id": task.id, "changes_summary": "Implemented structured verdicts."},
        )

    llm_service.call_json_feature.assert_awaited_once()
    tool_chat.assert_not_awaited()
    return result, record_iteration, llm_service.call_json_feature


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_task_ignores_failure_vocabulary_when_structured_verdict_is_valid(
    make_task_validator: Callable[..., TaskValidator],
    mock_task_manager: MagicMock,
    repo_path: str,
) -> None:
    narrative = "The script sets FAILED=1 on failure; all focused checks pass."

    result, _, call_json = await _close_with_static_verdict(
        make_task_validator,
        mock_task_manager,
        repo_path,
        {
            "status": "valid",
            "feedback": narrative,
            "blocking_reasons": [],
            "current_failure_evidence": [],
            "criterion_results": [
                {
                    "criterion": "Focused tests pass.",
                    "status": "satisfied",
                    "evidence_ids": [],
                    "explanation": "The linked diff provides the required evidence.",
                }
            ],
        },
    )

    assert result["success"] is True, result
    assert "verdict_override" not in result
    assert "FAILED=1" in call_json.call_args.args[1]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_required_command_evidence_blocks_close_and_matches_history(
    make_task_validator: Callable[..., TaskValidator],
    mock_task_manager: MagicMock,
    repo_path: str,
) -> None:
    result, record_iteration, _ = await _close_with_static_verdict(
        make_task_validator,
        mock_task_manager,
        repo_path,
        {
            "status": "invalid",
            "feedback": "A required command result is absent.",
            "blocking_reasons": [
                "Missing result for required command: uv run pytest tests/close_task.py"
            ],
            "current_failure_evidence": [],
            "criterion_results": [
                {
                    "criterion": "Focused tests pass.",
                    "status": "gap",
                    "evidence_ids": [],
                    "explanation": (
                        "Missing result for required command: uv run pytest tests/close_task.py"
                    ),
                }
            ],
        },
    )

    assert result["success"] is False
    assert result["message"].startswith("Close blocked: validation verdict 'invalid'")
    assert "Missing result for required command" in result["message"]
    persisted = mock_task_manager.increment_validation_failure.call_args.kwargs[
        "validation_feedback"
    ]
    history = record_iteration.call_args.kwargs["feedback"]
    assert result["message"] == persisted == history


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_task_exposes_structured_override_provenance(
    make_task_validator: Callable[..., TaskValidator],
    mock_task_manager: MagicMock,
    repo_path: str,
) -> None:
    result, _, _ = await _close_with_static_verdict(
        make_task_validator,
        mock_task_manager,
        repo_path,
        {
            "status": "valid",
            "feedback": "The implementation is complete, but a current check failed.",
            "blocking_reasons": [],
            "current_failure_evidence": ["pytest: 1 failed"],
            "criterion_results": [
                {
                    "criterion": "Focused tests pass.",
                    "status": "satisfied",
                    "evidence_ids": [],
                    "explanation": "The linked diff provides the required evidence.",
                }
            ],
        },
    )

    assert result["success"] is False
    assert result["verdict_override"] == {
        "from": "valid",
        "to": "invalid",
        "reason": "current_failure_evidence",
        "evidence": ["pytest: 1 failed"],
    }
    assert (
        "verdict overridden: validator attested current failures: pytest: 1 failed"
        in (result["message"])
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_task_uses_commit_diff_when_commits_linked(
    mock_task_manager: MagicMock, mock_task_validator: AsyncMock, repo_path: str
) -> None:
    """Test that close_task uses commit-based diff when task has linked commits."""
    task = _task(
        id="t1",
        title="Task with commits",
        project_id="p1",
        status="open",
        description="Do it",
        validation_criteria="Must have tests",
        commits=["abc123", "def456"],  # Has linked commits
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    mock_task_manager.get_task.return_value = task
    mock_task_manager.list_tasks.return_value = []

    mock_task_validator.validate_task.return_value = ValidationResult(status="valid", feedback="OK")
    mock_task_manager.close_task.return_value = task

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch("gobby.tasks.commits.collect_task_diff_text") as mock_diff,
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm,
        patch("gobby.utils.git.run_git_command", return_value="abc123"),
        patch(
            "gobby.utils.git.normalize_commit_sha",
            side_effect=lambda sha, cwd=None: sha,
        ),
    ):
        mock_diff.return_value = _diff_result(
            diff="diff content from commits",
            commits=["abc123", "def456"],
            has_uncommitted_changes=False,
            file_count=3,
        )
        mock_pm.return_value.get.return_value = MagicMock(repo_path=repo_path)

        registry = create_task_registry(
            task_manager=mock_task_manager,
            task_validator=mock_task_validator,
        )

        result = await registry.call(
            "close_task",
            {
                "task_id": "t1",
                "changes_summary": "test changes",
            },
        )

        validator_call = mock_task_validator.validate_task.call_args
        changes_summary = validator_call.kwargs["changes_summary"]
        mock_diff.assert_called_once()
        assert "Commit-based diff (2 commits, 3 manifest entries):" in changes_summary
        assert "diff content from commits" in changes_summary
        assert "Agent Changes Summary (supplemental):\ntest changes" in changes_summary
        assert result.get("validated", True) is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_task_includes_durable_receipt_packet_and_completeness(
    mock_task_manager: MagicMock,
    mock_task_validator: AsyncMock,
    repo_path: str,
) -> None:
    """close_task should pass all durable receipt identities and disclose any aggregation."""
    task = _task(
        id="t1",
        title="Task with retained evidence",
        project_id="p1",
        status="open",
        description="Do it",
        validation_criteria="Must have validation evidence",
        commits=["abc123"],
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    mock_task_manager.get_task.return_value = task
    mock_task_manager.list_tasks.return_value = []
    mock_task_validator.validate_task.return_value = ValidationResult(status="valid", feedback="OK")
    mock_task_manager.close_task.return_value = task

    timestamp = utc_now()
    receipts = [
        VerificationReceipt(
            id=f"receipt-{index:03d}",
            project_id="p1",
            session_id="sess-uuid",
            task_id="t1",
            provider="codex",
            execution_id=f"execution-{index:03d}",
            source_event_id=f"event-{index:03d}",
            evidence_type="shell_command",
            command=f"uv run pytest validation_suite_{index:03d}.py",
            cwd=repo_path,
            normalized_outcome="success",
            outcome_provenance="tool_output.json.exit_code",
            exit_code=0,
            started_at=timestamp + timedelta(seconds=index),
            completed_at=timestamp + timedelta(seconds=index),
            output_first_4k="passed",
            output_last_4k="passed",
            output_sha256=None,
            output_bytes=6,
            validation_epoch=0,
            details={},
            attribution_source="sole_claim",
            attribution_actor="sess-uuid",
            attributed_at=timestamp + timedelta(seconds=index),
            created_at=timestamp + timedelta(seconds=index),
            updated_at=timestamp + timedelta(seconds=index),
        )
        for index in range(1, 304)
    ]
    receipt_store = MagicMock()
    receipt_store.list_for_task.return_value = receipts
    receipt_store.count_unassigned.return_value = 3
    receipt_store.upsert.side_effect = _receipt_from_write

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch("gobby.tasks.commits.collect_task_diff_text") as mock_diff,
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as mock_sm_cls,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as mock_svm_cls,
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.VerificationReceiptStore",
            return_value=receipt_store,
        ),
        patch("gobby.utils.git.normalize_commit_sha", side_effect=lambda sha, cwd=None: sha),
    ):
        mock_diff.return_value = _diff_result(
            diff="diff content from commits",
            commits=["abc123"],
            has_uncommitted_changes=False,
            file_count=1,
        )
        mock_pm.return_value.get.return_value = MagicMock(repo_path=repo_path)
        mock_sm = MagicMock()
        mock_sm.resolve_session_reference.return_value = "sess-uuid"
        mock_sm.get.return_value = MagicMock(had_edits=True)
        mock_sm_cls.return_value = mock_sm
        mock_svm_cls.return_value.get_variables.return_value = {}

        registry = create_task_registry(
            task_manager=mock_task_manager,
            task_validator=mock_task_validator,
        )

        result = await registry.call(
            "close_task",
            {
                "task_id": "t1",
                "changes_summary": "Explicitly prioritize receipt-001",
            },
        )

    assert result["success"] is True
    receipt_store.list_for_task.assert_called_once_with("p1", "t1")
    completeness = result["evidence_completeness"]
    assert completeness["total"] == 304
    assert completeness["detailed"] == 12
    assert completeness["catalogued"] + completeness["aggregated"] == 304
    assert completeness["aggregated"] > 0
    assert completeness["unassigned"] == 3
    assert completeness["per_outcome"] == {"success": 304}
    validator_call = mock_task_validator.validate_task.call_args
    assert "Verification receipt packet" not in validator_call.kwargs["changes_summary"]
    validation_evidence = validator_call.kwargs["verification_receipt_text"]
    assert '"receipt_id":"receipt-001"' in validation_evidence
    assert '"receipt_id":"receipt-303"' in validation_evidence
    assert '"total":304' in validation_evidence
    packet_payload = json.loads(validation_evidence.removeprefix("Verification receipt packet:\n"))
    assert packet_payload["detailed_receipts"][0]["receipt_id"] == "receipt-001"
    projection = packet_payload["canonical_outcome_projection"]
    assert projection["total"] == 304
    assert projection["per_outcome"] == {"success": 304}
    assert projection["diagnostic_success_present"] is True
    assert projection["latest_receipt_id"] == "receipt-303"
    assert projection["latest_timestamp"] is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_task_accepts_git_diff_check_evidence_without_override(
    mock_task_manager: MagicMock,
    mock_task_validator: AsyncMock,
    repo_path: str,
) -> None:
    task = _task(
        id="t1",
        title="Task requiring a whitespace check",
        project_id="p1",
        status="open",
        description="Keep the patch free of whitespace errors",
        validation_criteria="A successful git diff --check result is required",
        commits=["abc123"],
        priority=2,
        task_type="bug",
        category="code",
        created_at="now",
        updated_at="now",
    )
    mock_task_manager.get_task.return_value = task
    mock_task_manager.list_tasks.return_value = []
    mock_task_manager.close_task.return_value = task
    mock_task_validator.validate_task.return_value = ValidationResult(
        status="valid",
        feedback="Required git diff --check evidence passed.",
    )
    receipt_store = MagicMock()
    receipt_store.list_for_task.return_value = [
        _verification_receipt("git diff --check HEAD~1..HEAD")
    ]
    receipt_store.upsert.side_effect = _receipt_from_write
    receipt_store.count_unassigned.return_value = 0

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch("gobby.tasks.commits.collect_task_diff_text") as mock_diff,
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as mock_sm_cls,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as mock_svm_cls,
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.VerificationReceiptStore",
            return_value=receipt_store,
        ),
        patch("gobby.utils.git.normalize_commit_sha", side_effect=lambda sha, cwd=None: sha),
    ):
        mock_diff.return_value = _diff_result(
            diff="diff content from commits",
            commits=["abc123"],
            has_uncommitted_changes=False,
            file_count=1,
        )
        mock_pm.return_value.get.return_value = MagicMock(repo_path=repo_path)
        mock_sm = MagicMock()
        mock_sm.resolve_session_reference.return_value = "sess-uuid"
        mock_sm.get.return_value = MagicMock(had_edits=True)
        mock_sm_cls.return_value = mock_sm
        mock_svm_cls.return_value.get_variables.return_value = {}

        registry = create_task_registry(
            task_manager=mock_task_manager,
            task_validator=mock_task_validator,
        )
        result = await registry.call(
            "close_task",
            {"task_id": "t1", "changes_summary": "Added Git validation classification"},
        )

    assert result["success"] is True
    validation_context = mock_task_validator.validate_task.call_args.kwargs[
        "verification_receipt_text"
    ]
    assert '"command":"git diff --check HEAD~1..HEAD"' in validation_context
    assert '"exit_code":0' in validation_context
    packet = json.loads(validation_context.removeprefix("Verification receipt packet:\n"))
    projection = packet["canonical_outcome_projection"]
    assert projection["diagnostic_success_present"] is True
    assert projection["per_outcome"] == {"success": 2}
    assert "matcher_id" not in validation_context
    close_kwargs = mock_task_manager.close_task.call_args.kwargs
    assert close_kwargs["validation_override_reason"] is None


@pytest.mark.asyncio
async def test_close_task_preserves_oversized_test_definitions_with_focused_evidence(
    mock_task_manager: MagicMock,
    mock_task_validator: AsyncMock,
    repo_path: str,
) -> None:
    """Close validation receives every acceptance test name and its passing command."""
    task = _task(
        id="t1",
        title="Task with oversized regression coverage",
        project_id="p1",
        status="open",
        description="Preserve acceptance tests in validation evidence",
        validation_criteria="All twelve acceptance tests and focused pytest evidence are visible",
        commits=["abc123"],
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    mock_task_manager.get_task.return_value = task
    mock_task_manager.list_tasks.return_value = []
    mock_task_validator.validate_task.return_value = ValidationResult(status="valid", feedback="OK")
    mock_task_manager.close_task.return_value = task

    helper_names = [f"helper_fixture_{index:02d}" for index in range(7)]
    test_names = [f"test_acceptance_{index:02d}" for index in range(12)]
    test_lines = [f"+test_file_line_{line:03d}_{'t' * 40}" for line in range(331)]
    for index, name in enumerate(helper_names):
        test_lines[45 + index * 10] = f"+def {name}(): pass"
    for index, name in enumerate(test_names[:-1]):
        test_lines[125 + index * 16] = f"+def {name}(): pass"
    test_lines[315] = f"+def {test_names[-1]}(): pass"

    production_lines = "".join(f"+production_line_{line:03d}_{'p' * 80}\n" for line in range(240))
    oversized_diff = (
        "diff --git a/src/large.py b/src/large.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/large.py\n"
        "@@ -0,0 +1,240 @@\n"
        + production_lines
        + "diff --git a/tests/test_acceptance.py b/tests/test_acceptance.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/test_acceptance.py\n"
        "@@ -0,0 +1,331 @@\n" + "".join(f"{line}\n" for line in test_lines)
    )
    focused_command = "GOBBY_TEST_PROTECT=1 uv run pytest tests/test_acceptance.py -k acceptance -q"
    receipt_store = MagicMock()
    receipt_store.list_for_task.return_value = [_verification_receipt(focused_command)]
    receipt_store.count_unassigned.return_value = 0
    receipt_store.upsert.side_effect = _receipt_from_write

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch("gobby.tasks.commits.collect_task_diff_text") as mock_diff,
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as mock_sm_cls,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as mock_svm_cls,
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.VerificationReceiptStore",
            return_value=receipt_store,
        ),
        patch("gobby.utils.git.normalize_commit_sha", side_effect=lambda sha, cwd=None: sha),
    ):
        mock_diff.return_value = _diff_result(
            diff=oversized_diff,
            commits=["abc123"],
            has_uncommitted_changes=False,
            file_count=2,
        )
        mock_pm.return_value.get.return_value = MagicMock(repo_path=repo_path)
        mock_sm = MagicMock()
        mock_sm.resolve_session_reference.return_value = "sess-uuid"
        mock_sm.get.return_value = MagicMock(had_edits=True)
        mock_sm_cls.return_value = mock_sm
        mock_svm_cls.return_value.get_variables.return_value = {}

        registry = create_task_registry(
            task_manager=mock_task_manager,
            task_validator=mock_task_validator,
        )

        result = await registry.call(
            "close_task", {"task_id": "t1", "changes_summary": "test changes"}
        )
        validator_call = mock_task_validator.validate_task.call_args
        changes_summary = validator_call.kwargs["changes_summary"]
        receipt_text = validator_call.kwargs["verification_receipt_text"]

    assert result["success"] is True
    for name in test_names:
        assert name in changes_summary
    assert "hunk truncated for tests/test_acceptance.py" in changes_summary
    assert f'"command":"{focused_command}"' in receipt_text
    packet = json.loads(receipt_text.removeprefix("Verification receipt packet:\n"))
    projection = packet["canonical_outcome_projection"]
    assert projection["diagnostic_success_present"] is True
    assert projection["per_outcome"] == {"success": 2}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_task_autolinks_claim_window_before_validation(
    mock_task_manager: MagicMock, mock_task_validator: AsyncMock, repo_path: str
) -> None:
    """close_task(commit_sha=last) validates the resolved linked task commit set."""
    task = _task(
        id="t1",
        title="Task with interleaved commits",
        project_id="p1",
        status="open",
        description="Do it",
        validation_criteria="Must include only task commits",
        commits=["a1"],
        claimed_by_session_id="sess-uuid",
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    mock_task_manager.get_task.return_value = task
    mock_task_manager.list_tasks.return_value = []
    mock_task_validator.validate_task.return_value = ValidationResult(status="valid", feedback="OK")
    mock_task_manager.close_task.return_value = task

    def link_commit_side_effect(task_id: str, commit_sha: str, cwd: str | None = None) -> Task:
        assert task_id == "t1"
        assert cwd == repo_path
        assert task.commits is not None
        task.commits.append(commit_sha)
        return task

    def resolve_commits_side_effect(*args: Any, **kwargs: Any) -> list[str]:
        assert kwargs["task_id"] == "t1"
        assert kwargs["since"] == "2026-05-01T00:00:00+00:00"
        assert kwargs["cwd"] == repo_path
        assert kwargs["project_id"] == "p1"
        return ["a2"]

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as mock_sm_cls,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager") as mock_stm_cls,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as mock_svm_cls,
        patch("gobby.tasks.commits.resolve_task_tagged_commits") as mock_resolve_commits,
        patch("gobby.tasks.commits.collect_task_diff_text") as mock_diff,
        patch("gobby.utils.git.normalize_commit_sha", side_effect=lambda sha, cwd=None: sha),
    ):
        mock_pm.return_value.get.return_value = MagicMock(repo_path=repo_path)
        mock_sm = MagicMock()
        mock_sm.resolve_session_reference.return_value = "sess-uuid"
        mock_sm.get.return_value = MagicMock(had_edits=True)
        mock_sm_cls.return_value = mock_sm
        mock_stm = MagicMock()
        mock_stm.get_task_sessions.return_value = [
            {
                "session_id": "sess-uuid",
                "task_id": "t1",
                "action": "claimed",
                "created_at": "2026-05-01T00:00:00+00:00",
            }
        ]
        mock_stm_cls.return_value = mock_stm
        mock_svm_cls.return_value.get_variables.return_value = {}
        mock_task_manager.link_commit.side_effect = link_commit_side_effect
        mock_resolve_commits.side_effect = resolve_commits_side_effect

        def diff_side_effect(
            task_id: str,
            task_manager: MagicMock,
            include_uncommitted: bool,
            cwd: str,
        ) -> tuple[str, dict[str, dict[str, int]]]:
            assert task.commits is not None
            assert task.commits == ["a1", "a2", "a3"]
            assert include_uncommitted is False
            assert cwd == repo_path
            return _diff_result(
                diff=(
                    "diff --git a/a1.py b/a1.py\n+task A1\n"
                    "diff --git a/a2.py b/a2.py\n+task A2\n"
                    "diff --git a/a3.py b/a3.py\n+task A3\n"
                ),
                commits=list(task.commits),
                has_uncommitted_changes=False,
                file_count=3,
            )

        mock_diff.side_effect = diff_side_effect

        registry = create_task_registry(
            task_manager=mock_task_manager,
            task_validator=mock_task_validator,
        )

        result = await registry.call(
            "close_task",
            {
                "task_id": "t1",
                "commit_sha": "a3",
                "changes_summary": "test changes",
            },
        )
        validator_call = mock_task_validator.validate_task.call_args
        changes_summary = validator_call.kwargs["changes_summary"]

    assert result["success"] is True
    mock_resolve_commits.assert_called_once()
    assert "Commit-based diff (3 commits, 3 manifest entries):" in changes_summary
    assert "task A1" in changes_summary
    assert "task A2" in changes_summary
    assert "task A3" in changes_summary
    assert "foreign" not in changes_summary


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_task_rejects_skip_validation_with_override_justification(
    mock_task_manager: MagicMock,
    mock_task_validator: AsyncMock,
) -> None:
    task = _task(
        id="t1",
        title="Task with audited override",
        project_id="p1",
        status="open",
        description="Do it",
        validation_criteria="Must have tests",
        commits=["abc123"],
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    mock_task_manager.get_task.return_value = task
    mock_task_manager.list_tasks.return_value = []
    registry = create_task_registry(
        task_manager=mock_task_manager,
        task_validator=mock_task_validator,
    )
    result = await registry.call(
        "close_task",
        {
            "task_id": "t1",
            "skip_validation": True,
            "override_justification": "Validator unavailable; exact diff reviewed.",
            "changes_summary": "test changes",
        },
    )

    assert result == {
        "success": False,
        "error": "validation_contract_not_skippable",
        "message": "Non-epic task close cannot skip criterion-to-evidence validation.",
    }
    mock_task_validator.validate_task.assert_not_called()
    mock_task_manager.close_task.assert_not_called()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_task_skip_validation_fails_without_evidence(
    mock_task_manager: MagicMock,
    mock_task_validator: AsyncMock,
) -> None:
    task = _task(
        id="t1",
        title="Task missing evidence",
        project_id="p1",
        status="open",
        description="Do it",
        validation_criteria="Must have tests",
        commits=["abc123"],
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    mock_task_manager.get_task.return_value = task
    mock_task_manager.list_tasks.return_value = []

    registry = create_task_registry(
        task_manager=mock_task_manager,
        task_validator=mock_task_validator,
    )
    result = await registry.call(
        "close_task",
        {
            "task_id": "t1",
            "skip_validation": True,
            "changes_summary": "test changes",
        },
    )

    assert result["success"] is False
    assert result["error"] == "validation_contract_not_skippable"
    mock_task_manager.close_task.assert_not_called()
    mock_task_validator.validate_task.assert_not_called()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_task_skip_reason_bypasses_commit_check_but_runs_validation(
    mock_task_manager: MagicMock, mock_task_validator: AsyncMock, repo_path: str
) -> None:
    """Skip reasons bypass commit requirements but retain the validation contract."""
    task = _task(
        id="t1",
        title="Task without commits",
        project_id="p1",
        status="open",
        description="Do it",
        validation_criteria="Must have tests",
        commits=None,  # No commits
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    mock_task_manager.get_task.return_value = task
    mock_task_manager.list_tasks.return_value = []

    mock_task_validator.validate_task.return_value = ValidationResult(status="valid", feedback="OK")
    mock_task_manager.close_task.return_value = task

    def git_command_side_effect(cmd: list[str], cwd: str | None = None) -> str:
        """Return appropriate values for different git commands."""
        if "rev-parse" in cmd:
            # Return commit SHA for rev-parse
            return "abc123"
        return ""

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm,
        patch("gobby.utils.git.run_git_command", side_effect=git_command_side_effect),
    ):
        mock_pm.return_value.get.return_value = MagicMock(repo_path=repo_path)

        registry = create_task_registry(
            task_manager=mock_task_manager,
            task_validator=mock_task_validator,
        )

        # Using reason="obsolete" bypasses the commit requirement.
        result = await registry.call(
            "close_task",
            {
                "task_id": "t1",
                "reason": "obsolete",
                "changes_summary": "test changes",
            },
        )

        # Should succeed without error
        assert "error" not in result
        # close_task should have been called
        mock_task_manager.close_task.assert_called_once()
        assert mock_task_manager.close_task.call_count == 1
        assert mock_task_manager.close_task.call_args is not None
        mock_task_validator.validate_task.assert_awaited_once()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_task_commit_diff_excludes_uncommitted_changes(
    mock_task_manager: MagicMock, mock_task_validator: AsyncMock, repo_path: str
) -> None:
    """Test that close_task excludes uncommitted changes (linked commits are the work)."""
    task = _task(
        id="t1",
        title="Task with commits",
        project_id="p1",
        status="open",
        description="Do it",
        validation_criteria="Must have tests",
        commits=["abc123"],
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    mock_task_manager.get_task.return_value = task
    mock_task_manager.list_tasks.return_value = []

    mock_task_validator.validate_task.return_value = ValidationResult(status="valid", feedback="OK")
    mock_task_manager.close_task.return_value = task

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch("gobby.tasks.commits.collect_task_diff_text") as mock_diff,
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm,
        patch("gobby.utils.git.run_git_command", return_value="abc123"),
    ):
        mock_diff.return_value = _diff_result(
            diff="diff content plus uncommitted",
            commits=["abc123"],
            has_uncommitted_changes=True,
            file_count=5,
        )
        mock_pm.return_value.get.return_value = MagicMock(repo_path=repo_path)

        registry = create_task_registry(
            task_manager=mock_task_manager,
            task_validator=mock_task_validator,
        )

        result = await registry.call(
            "close_task",
            {"task_id": "t1", "changes_summary": "test changes"},
        )

        assert result["success"] is True
        validator_call = mock_task_validator.validate_task.call_args
        changes_summary = validator_call.kwargs["changes_summary"]
        assert "Commit-based diff (1 commits, 5 manifest entries):" in changes_summary
        mock_diff.assert_called_once_with(
            task_id="t1",
            task_manager=mock_task_manager,
            include_uncommitted=False,
            cwd=repo_path,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_task_with_commits_does_not_fallback_to_smart_context(
    mock_task_manager: MagicMock, mock_task_validator: AsyncMock, repo_path: str
) -> None:
    """Test that close_task with linked commits doesn't fall back to smart context.

    When a task has linked commits, those commits ARE the work, so we don't
    fall back to smart context even if the diff is empty.
    """
    task = _task(
        id="t1",
        title="Task with commits but empty diff",
        project_id="p1",
        status="open",
        description="Do it",
        validation_criteria="Must have tests",
        commits=["abc123"],
        priority=2,
        task_type="task",
        created_at="now",
        updated_at="now",
    )
    mock_task_manager.get_task.return_value = task
    mock_task_manager.list_tasks.return_value = []

    mock_task_validator.validate_task.return_value = ValidationResult(status="valid", feedback="OK")
    mock_task_manager.close_task.return_value = task

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch("gobby.tasks.commits.collect_task_diff_text") as mock_diff,
        patch("gobby.tasks.validation.get_validation_context_smart") as mock_smart_context,
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm,
        patch("gobby.utils.git.run_git_command", return_value="abc123"),
    ):
        # Empty diff from commits
        mock_diff.return_value = _diff_result(
            diff="",
            commits=["abc123"],
            has_uncommitted_changes=False,
            file_count=0,
        )
        mock_smart_context.return_value = "Smart context as fallback for empty diff"
        mock_pm.return_value.get.return_value = MagicMock(repo_path=repo_path)

        registry = create_task_registry(
            task_manager=mock_task_manager,
            task_validator=mock_task_validator,
        )

        await registry.call("close_task", {"task_id": "t1", "changes_summary": "test changes"})

        mock_diff.assert_called_once_with(
            task_id="t1",
            task_manager=mock_task_manager,
            include_uncommitted=False,
            cwd=repo_path,
        )
        mock_smart_context.assert_not_called()
        assert mock_smart_context.call_count == 0
        assert not mock_smart_context.called


@pytest.mark.asyncio
async def test_close_task_blocked_preview_is_read_only_and_ready_preview_closes(
    mock_task_manager: MagicMock,
    mock_task_validator: AsyncMock,
    repo_path: str,
) -> None:
    task = _task(
        id="t1",
        title="Preview close",
        project_id="p1",
        status="open",
        description="Preview the canonical close evaluator.",
        validation_criteria="Focused tests pass.",
        commits=["a1"],
        claimed_by_session_id="sess-uuid",
        priority=1,
        task_type="task",
        category="code",
        created_at="now",
        updated_at="now",
    )
    receipts = [
        _verification_receipt(f"uv run pytest tests/test_{index}.py", index=index)
        for index in range(1, 21)
    ]
    receipts.append(
        _verification_receipt(
            "uv run pytest tests/failing.py",
            index=21,
            normalized_outcome="failure",
            exit_code=1,
        )
    )
    mock_task_manager.get_task.return_value = task
    mock_task_manager.list_tasks.return_value = []
    mock_task_validator.validate_task.return_value = ValidationResult(
        status="valid",
        feedback="All focused checks pass.",
    )

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as mock_sm_cls,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager") as mock_stm_cls,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as mock_svm_cls,
        patch("gobby.tasks.commits.resolve_task_tagged_commits", return_value=["a2"]),
        patch(
            "gobby.tasks.commits.collect_task_diff_text",
            return_value=_diff_result(
                diff="diff --git a/a.py b/a.py\n+preview\n",
                commits=["a1", "a2", "a3"],
                file_count=1,
            ),
        ),
        patch("gobby.utils.git.normalize_commit_sha", side_effect=lambda sha, cwd=None: sha),
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_validation._record_validation_iteration"
        ) as record_iteration,
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.VerificationReceiptStore"
        ) as receipt_store_cls,
    ):
        mock_pm.return_value.get.return_value = MagicMock(repo_path=repo_path)
        mock_sm_cls.return_value.resolve_session_reference.return_value = "sess-uuid"
        mock_stm_cls.return_value.get_task_sessions.return_value = [
            {
                "session_id": "sess-uuid",
                "task_id": "t1",
                "action": "claimed",
                "created_at": "2026-05-01T00:00:00+00:00",
            }
        ]
        mock_svm_cls.return_value.get_variables.return_value = {}
        receipt_store_cls.return_value.list_for_task.return_value = receipts
        receipt_store_cls.return_value.count_unassigned.return_value = 3
        receipt_store_cls.return_value.upsert.side_effect = _receipt_from_write
        registry = create_task_registry(
            task_manager=mock_task_manager,
            task_validator=mock_task_validator,
        )

        missing_result = await registry.call(
            "close_task",
            {
                "task_id": "t1",
                "commit_sha": "a3",
                "changes_summary": "Implemented preview.",
                "preview": True,
                "evidence_receipt_ids": ["missing-receipt"],
            },
        )
        assert missing_result["preview"] is True
        assert missing_result["can_close"] is False
        assert missing_result["closed"] is False
        mock_task_manager.link_commit.assert_not_called()
        mock_task_manager.close_task.assert_not_called()
        mock_task_manager.update_task.assert_not_called()
        mock_task_manager.increment_validation_failure.assert_not_called()
        receipt_store_cls.return_value.upsert.assert_not_called()
        mock_svm_cls.return_value.merge_variables.assert_not_called()
        record_iteration.assert_not_called()

        result = await registry.call(
            "close_task",
            {
                "task_id": "t1",
                "commit_sha": "a3",
                "changes_summary": "Implemented preview.",
                "preview": True,
                "response_detail": "diagnostic",
                "evidence_receipt_ids": ["receipt-020"],
            },
        )

    assert missing_result["success"] is True
    assert missing_result["can_close"] is False
    assert missing_result["error"] == "evidence_receipts_not_found"
    assert "assign the intended receipt IDs" in missing_result["required_actions"][0]
    assert "mechanical_gates" not in missing_result
    assert "selected_evidence" not in missing_result
    assert "evidence_completeness" not in missing_result
    assert result["success"] is True
    assert result["preview"] is True
    assert result["can_close"] is True
    assert result["closed"] is True
    assert result["commit_shas"] == ["a1", "a2", "a3"]
    assert "receipt-020" in result["selected_evidence"]["detailed_receipt_ids"]
    assert "receipt-019" in result["selected_evidence"]["catalogued_receipt_ids"]
    assert "receipt-021" not in result["selected_evidence"]["catalogued_receipt_ids"]
    assert result["evidence_completeness"]["total"] == 21
    assert result["unassigned_receipts"]["count"] == 3
    assert result["blocking_reasons"] == []
    assert result["required_actions"] == []
    receipt_text = mock_task_validator.validate_task.await_args.kwargs["verification_receipt_text"]
    assert "receipt-020" in receipt_text
    assert "receipt-021" not in receipt_text
    assert mock_task_manager.link_commit.call_count > 0
    mock_task_manager.close_task.assert_called_once()
    mock_task_manager.update_task.assert_not_called()
    mock_task_manager.increment_validation_failure.assert_not_called()
    receipt_store_cls.return_value.upsert.assert_called_once()
    mock_svm_cls.return_value.merge_variables.assert_called_once()
    record_iteration.assert_called_once()


@pytest.mark.asyncio
async def test_conditional_close_reevaluates_and_reports_latest_failure(
    mock_task_manager: MagicMock,
    mock_task_validator: AsyncMock,
    repo_path: str,
) -> None:
    task = _task(
        id="t1",
        title="Reevaluate close",
        project_id="p1",
        status="open",
        description="Reevaluate current evidence.",
        validation_criteria="Focused tests pass.",
        commits=["a1"],
        claimed_by_session_id="sess-uuid",
        priority=1,
        task_type="task",
        category="code",
        created_at="now",
        updated_at="now",
    )
    mock_task_manager.get_task.return_value = task
    mock_task_manager.list_tasks.return_value = []
    mock_task_manager.increment_validation_failure.return_value = (1, False)
    mock_task_validator.validate_task.side_effect = [
        ValidationResult(status="valid", feedback="First pass."),
        ValidationResult(
            status="invalid",
            feedback="Evidence changed.",
            blocking_reasons=["Focused tests no longer pass."],
            failure_category=FailureCategory.TEST,
        ),
    ]

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as mock_sm_cls,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as mock_svm_cls,
        patch(
            "gobby.tasks.commits.collect_task_diff_text",
            return_value=_diff_result(
                diff="diff --git a/a.py b/a.py\n+change\n",
                commits=["a1"],
                file_count=1,
            ),
        ),
        patch("gobby.utils.git.normalize_commit_sha", side_effect=lambda sha, cwd=None: sha),
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_close.VerificationReceiptStore"
        ) as receipt_store_cls,
        patch(
            "gobby.mcp_proxy.tools.tasks._lifecycle_validation._record_validation_iteration"
        ) as record_iteration,
    ):
        mock_pm.return_value.get.return_value = MagicMock(repo_path=repo_path)
        mock_sm_cls.return_value.resolve_session_reference.return_value = "sess-uuid"
        mock_svm_cls.return_value.get_variables.return_value = {}
        receipt_store_cls.return_value.list_for_task.return_value = [
            _verification_receipt("uv run pytest tests/focused.py")
        ]
        receipt_store_cls.return_value.count_unassigned.return_value = 0
        receipt_store_cls.return_value.upsert.side_effect = _receipt_from_write
        registry = create_task_registry(
            task_manager=mock_task_manager,
            task_validator=mock_task_validator,
        )

        result = await registry.call(
            "close_task",
            {
                "task_id": "t1",
                "changes_summary": "Implemented reevaluation.",
                "preview": True,
            },
        )

    assert result["success"] is False
    assert result["preview"] is True
    assert result["can_close"] is False
    assert result["closed"] is False
    assert result["error"] == "validation_failed"
    assert result["blocking_reasons"]
    assert mock_task_validator.validate_task.await_count == 2
    mock_task_manager.increment_validation_failure.assert_called_once()
    record_iteration.assert_called_once()
    mock_task_manager.close_task.assert_not_called()


@pytest.mark.asyncio
async def test_close_task_reconciles_paired_codex_receipts_for_preview_and_mutation(
    mock_task_manager: MagicMock,
    mock_task_validator: AsyncMock,
    repo_path: str,
) -> None:
    commands = [
        "uv run pytest tests/tasks/test_verification_receipt_packet.py -q",
        "uv run ruff check src/gobby/tasks",
        "uv run mypy src/gobby/tasks",
    ]
    receipts = _paired_codex_receipts(commands)
    result, packets = await _preview_and_close_with_receipts(
        mock_task_manager,
        mock_task_validator,
        repo_path,
        receipts,
        expected_successes=len(commands),
    )

    assert result["success"] is True
    assert result["preview"] is True
    assert result["can_close"] is True
    assert result["closed"] is True
    assert result["validation_status"] == "valid"
    assert len(packets) == 2
    assert packets[0] == packets[1]
    packet_payload = json.loads(packets[0].removeprefix("Verification receipt packet:\n"))
    projection = packet_payload["canonical_outcome_projection"]
    assert projection["per_outcome"] == {"success": 4}
    assert projection["raw_per_outcome"] == {"success": 4}
    assert projection["superseded_total"] == 0
    assert packet_payload["evidence_completeness"]["effective_total"] == 4
    assert all(receipt.id not in packets[0] for receipt in receipts[::2])
    mock_task_manager.close_task.assert_called_once()


@pytest.mark.asyncio
async def test_close_task_keeps_unknown_only_required_command_pending(
    mock_task_manager: MagicMock,
    mock_task_validator: AsyncMock,
    repo_path: str,
) -> None:
    unknown_receipt = _verification_receipt(
        "uv run pytest tests/focused.py -q",
        normalized_outcome="unknown",
        exit_code=None,
        execution_id="exec-unknown-only",
        outcome_provenance="before_tool",
        output="",
    )
    result, packets = await _preview_and_close_with_receipts(
        mock_task_manager,
        mock_task_validator,
        repo_path,
        [unknown_receipt],
        expected_successes=1,
    )

    assert result["success"] is True
    assert result["preview"] is True
    assert result["can_close"] is False
    assert result["closed"] is False
    assert result["validation_status"] == "pending"
    packet = json.loads(packets[0].removeprefix("Verification receipt packet:\n"))
    projection = packet["canonical_outcome_projection"]
    assert projection["per_outcome"] == {"success": 1}
    assert projection["diagnostic_success_present"] is True
    assert projection["superseded_total"] == 0
    mock_task_manager.close_task.assert_not_called()
