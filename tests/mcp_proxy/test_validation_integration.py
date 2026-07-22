import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.llm import LLMService
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._verification_evidence_context import (
    format_verification_evidence_context,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, StageState, Task
from gobby.storage.verification_receipts import VerificationReceipt
from gobby.tasks.validation import TaskValidator
from gobby.tasks.validation_verdict import ValidationResult
from gobby.utils.datetime import utc_now
from gobby.utils.session_context import session_context_for_test


def _task_validator(
    config: TaskValidationConfig,
    llm_service: LLMService,
    **kwargs: Any,
) -> TaskValidator:
    return TaskValidator(config, llm_service, db=MagicMock(spec=HubDatabase), **kwargs)


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
) -> VerificationReceipt:
    timestamp = utc_now() + timedelta(seconds=index)
    return VerificationReceipt(
        id=f"receipt-{index:03d}",
        project_id="p1",
        session_id="sess-uuid",
        task_id="t1",
        provider="codex",
        execution_id=f"execution-{index:03d}",
        source_event_id=f"event-{index:03d}",
        evidence_type="shell_command",
        command=command,
        cwd="/repo",
        normalized_outcome="success",
        outcome_provenance="tool_output.json.exit_code",
        exit_code=0,
        started_at=timestamp,
        completed_at=timestamp,
        output_first_4k="passed",
        output_last_4k="passed",
        output_sha256=None,
        output_bytes=6,
        details={},
        attribution_source="sole_claim",
        attribution_actor="sess-uuid",
        attributed_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
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


@pytest.fixture
def mock_task_manager() -> MagicMock:
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()  # Needed for dep_manager init
    manager.db.fetchone.return_value = None
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    manager.db.transaction.return_value.__enter__.return_value = conn
    return manager


@pytest.fixture
def mock_task_validator() -> AsyncMock:
    validator = AsyncMock(spec=TaskValidator)
    return validator


# ============================================================================
# Close Task with Commit-Based Validation Tests
# ============================================================================


async def _close_with_static_verdict(
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
    llm_service.call_json_feature = AsyncMock(return_value=payload)
    validator = _task_validator(
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
    mock_task_manager: MagicMock, repo_path: str
) -> None:
    narrative = "The script sets FAILED=1 on failure; all focused checks pass."

    result, _, call_json = await _close_with_static_verdict(
        mock_task_manager,
        repo_path,
        {
            "status": "valid",
            "feedback": narrative,
            "blocking_reasons": [],
            "current_failure_evidence": [],
        },
    )

    assert result["success"] is True
    assert "verdict_override" not in result
    assert "FAILED=1" in call_json.call_args.args[1]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_required_command_evidence_blocks_close_and_matches_history(
    mock_task_manager: MagicMock, repo_path: str
) -> None:
    result, record_iteration, _ = await _close_with_static_verdict(
        mock_task_manager,
        repo_path,
        {
            "status": "invalid",
            "feedback": "A required command result is absent.",
            "blocking_reasons": [
                "Missing result for required command: uv run pytest tests/close_task.py"
            ],
            "current_failure_evidence": [],
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
    mock_task_manager: MagicMock, repo_path: str
) -> None:
    result, _, _ = await _close_with_static_verdict(
        mock_task_manager,
        repo_path,
        {
            "status": "valid",
            "feedback": "The implementation is complete, but a current check failed.",
            "blocking_reasons": [],
            "current_failure_evidence": ["pytest: 1 failed"],
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
    completeness = result["evidence_completeness"]
    assert completeness["total"] == 303
    assert completeness["detailed"] == 12
    assert completeness["catalogued"] + completeness["aggregated"] == 303
    assert completeness["aggregated"] > 0
    assert completeness["unassigned"] == 3
    assert completeness["per_outcome"] == {"success": 303}
    validator_call = mock_task_validator.validate_task.call_args
    assert "Verification receipt packet" not in validator_call.kwargs["changes_summary"]
    validation_evidence = validator_call.kwargs["verification_receipt_text"]
    assert '"receipt_id":"receipt-001"' in validation_evidence
    assert '"receipt_id":"receipt-303"' in validation_evidence
    assert '"total":303' in validation_evidence
    packet_payload = json.loads(validation_evidence.removeprefix("Verification receipt packet:\n"))
    assert packet_payload["detailed_receipts"][0]["receipt_id"] == "receipt-001"
    projection = packet_payload["canonical_outcome_projection"]
    assert projection["total"] == 303
    assert projection["per_outcome"] == {"success": 303}
    assert projection["ready"] is True
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
    assert '"ready":true' in validation_context
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
    assert '"ready":true' in receipt_text


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

    def autolink_side_effect(*args: Any, **kwargs: Any) -> None:
        assert kwargs["task_id"] == "t1"
        assert kwargs["since"] == "2026-05-01T00:00:00+00:00"
        assert kwargs["cwd"] == repo_path
        assert kwargs["project_id"] == "p1"
        assert task.commits is not None
        task.commits.insert(1, "a2")

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as mock_sm_cls,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager") as mock_stm_cls,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as mock_svm_cls,
        patch("gobby.tasks.commits.auto_link_commits") as mock_autolink,
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
        mock_autolink.side_effect = autolink_side_effect

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
    mock_autolink.assert_called_once()
    assert "Commit-based diff (3 commits, 3 manifest entries):" in changes_summary
    assert "task A1" in changes_summary
    assert "task A2" in changes_summary
    assert "task A3" in changes_summary
    assert "foreign" not in changes_summary


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_task_skip_validation_with_evidence_stores_override(
    mock_task_manager: MagicMock, mock_task_validator: AsyncMock, repo_path: str
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
    mock_task_manager.close_task.return_value = task

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as mock_sm_cls,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as mock_svm_cls,
        patch("gobby.utils.git.normalize_commit_sha", side_effect=lambda sha, cwd=None: sha),
    ):
        mock_pm.return_value.get.return_value = MagicMock(repo_path=repo_path)
        mock_sm = MagicMock()
        mock_sm.resolve_session_reference.return_value = "sess-uuid"
        mock_sm.get.return_value = MagicMock(had_edits=True)
        mock_sm_cls.return_value = mock_sm
        mock_svm_cls.return_value.get_variables.return_value = {
            "verification_evidence": [
                {
                    "evidence_type": "manual_diff_review",
                    "success": True,
                    "summary": "Reviewed exact linked commits",
                    "supports": "completion readiness for t1",
                }
            ]
        }

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

    assert result == {"success": True}
    mock_task_validator.validate_task.assert_not_called()
    mock_task_manager.close_task.assert_called_once()
    assert (
        mock_task_manager.close_task.call_args.kwargs["validation_override_reason"]
        == "Validator unavailable; exact diff reviewed."
    )
    assert mock_task_manager.close_task.call_args.kwargs["reset_validation_fail_count"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_task_skip_validation_fails_without_evidence(
    mock_task_manager: MagicMock, mock_task_validator: AsyncMock, repo_path: str
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

    with (
        patch("gobby.mcp_proxy.tools.tasks._context.TaskDependencyManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.LocalProjectManager") as mock_pm,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as mock_sm_cls,
        patch("gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"),
        patch("gobby.mcp_proxy.tools.tasks._context.SessionVariableManager") as mock_svm_cls,
        patch("gobby.utils.git.normalize_commit_sha", side_effect=lambda sha, cwd=None: sha),
    ):
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
                "skip_validation": True,
                "override_justification": "Validator unavailable.",
                "changes_summary": "test changes",
            },
        )

    assert result["success"] is False
    assert result["error"] == "skip_validation_missing_evidence"
    mock_task_manager.close_task.assert_not_called()
    mock_task_validator.validate_task.assert_not_called()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_close_task_skip_reason_bypasses_commit_check(
    mock_task_manager: MagicMock, mock_task_validator: AsyncMock, repo_path: str
) -> None:
    """Test that close_task with skip reason (obsolete) bypasses commit check.

    When using a skip reason like 'obsolete', 'duplicate', 'already_implemented',
    or 'wont_fix', the commit check is skipped and validation is also auto-skipped.
    """
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

        # Using reason="obsolete" bypasses commit check and validation
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
        # Validator should NOT have been called (skip reasons auto-skip validation)
        mock_task_validator.validate_task.assert_not_called()
        assert mock_task_validator.validate_task.call_count == 0
        assert not mock_task_validator.validate_task.called


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
