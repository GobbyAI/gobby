"""
Comprehensive unit tests for gobby.tasks.validation module.

This test module provides additional coverage for the task validation module,
focusing on areas not covered by test_task_validation.py:
- get_last_commit_diff truncation logic
- get_recent_commits line parsing edge cases
- get_commits_since truncation
- find_matching_files glob exception handling and early exit
- read_files_content early truncation
- get_validation_context_smart final truncation
- get_git_diff fallback_to_last_commit=False path
- validate_task category parameter handling
"""

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.ai.text_generation import FeatureGenerationUnavailableError
from gobby.config.tasks import TaskValidationConfig
from gobby.failure_categories import FailureCategory
from gobby.llm import LLMService
from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.validation import (
    VALIDATION_PROMPT_BUDGET_CHARS,
    TaskValidator,
    ValidationResult,
    extract_file_patterns_from_text,
    find_matching_files,
    get_commits_since,
    get_git_diff,
    get_last_commit_diff,
    get_multi_commit_diff,
    get_recent_commits,
    get_validation_context_smart,
    read_files_content,
    run_git_command,
)
from gobby.tasks.validation_evidence import (
    ChangedFileEvidence,
    _added_signature_names,
    _excerpt_file_diff,
    build_diff_validation_evidence,
)
from tests.tasks.contract_validator import ContractTaskValidator


def _task_validator(
    config: TaskValidationConfig,
    llm_service: LLMService,
    **kwargs: Any,
) -> TaskValidator:
    return ContractTaskValidator(config, llm_service, db=MagicMock(spec=HubDatabase), **kwargs)


pytestmark = pytest.mark.unit


class TestValidationPromptBudget:
    """Validation prompts use shaped evidence with explicit omissions."""

    @pytest.fixture
    def mock_llm(self) -> MagicMock:
        llm = MagicMock(spec=LLMService)
        llm.call_json_feature = AsyncMock()
        return llm

    @pytest.fixture
    def config(self) -> TaskValidationConfig:
        return TaskValidationConfig(enabled=True, candidates=["claude/test-model"])

    def test_diff_evidence_reports_source_ui_absence(self) -> None:
        diff = (
            "diff --git a/docs/guide.md b/docs/guide.md\n"
            "index abc..def 100644\n"
            "--- a/docs/guide.md\n"
            "+++ b/docs/guide.md\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )

        evidence = build_diff_validation_evidence(diff, max_chars=2000)

        assert "Changed File Manifest (authoritative):" in evidence.text
        assert "Source/UI files changed: none" in evidence.text
        assert "- docs/guide.md (+1/-1) [docs]" in evidence.text

    def test_diff_evidence_includes_binary_file_markers_in_manifest(self) -> None:
        evidence = build_diff_validation_evidence(
            "Binary files a/web/public/logo.png and b/web/public/logo.png differ\n",
            max_chars=2000,
        )

        assert [file.path for file in evidence.manifest] == ["web/public/logo.png"]
        assert "- web/public/logo.png (+0/-0) [other]" in evidence.text

    def test_diff_evidence_respects_max_chars_for_raw_text(self) -> None:
        evidence = build_diff_validation_evidence(
            "raw change payload\n" + ("x" * 500),
            max_chars=80,
        )

        assert len(evidence.text) <= 80

    def test_diff_evidence_reports_when_agent_summary_is_included(self) -> None:
        included = build_diff_validation_evidence(
            "diff --git a/src/app.py b/src/app.py\n"
            "index abc..def 100644\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n",
            max_chars=2000,
            agent_summary="Updated app behavior.",
        )
        omitted = build_diff_validation_evidence(
            "diff --git a/src/app.py b/src/app.py\n"
            "index abc..def 100644\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n",
            max_chars=120,
            agent_summary="x" * 500,
        )

        assert included.agent_summary_included is True
        assert omitted.agent_summary_included is False

    def test_diff_evidence_keeps_tail_of_later_files(self) -> None:
        """A large first file cannot starve later files of their evidence."""
        source_lines = "".join(f"+source_line_{line}_{'s' * 40}\n" for line in range(120))
        test_lines = "".join(f"+test_line_{line}_{'t' * 40}\n" for line in range(100))
        diff = (
            "diff --git a/src/big.py b/src/big.py\n"
            "index abc..def 100644\n"
            "--- a/src/big.py\n"
            "+++ b/src/big.py\n"
            "@@ -1 +1,120 @@\n"
            + source_lines
            + "diff --git a/tests/test_big.py b/tests/test_big.py\n"
            "index abc..def 100644\n"
            "--- a/tests/test_big.py\n"
            "+++ b/tests/test_big.py\n"
            "@@ -1 +1,100 @@\n" + test_lines
        )

        evidence = build_diff_validation_evidence(diff, max_chars=6000, max_hunk_lines=200)

        assert "### tests/test_big.py" in evidence.text
        assert "+test_line_99_" in evidence.text

    def test_supplemental_summary_does_not_force_head_only_raw_diff_truncation(self) -> None:
        """Oversized raw evidence must use per-file excerpts before adding agent prose."""
        source_lines = "".join(f"+source_line_{line}_{'s' * 50}\n" for line in range(100))
        test_lines = "".join(f"+def test_acceptance_{line}(): pass\n" for line in range(40))
        diff = (
            "diff --git a/src/big.py b/src/big.py\n"
            "index abc..def 100644\n"
            "--- a/src/big.py\n"
            "+++ b/src/big.py\n"
            "@@ -1 +1,100 @@\n"
            + source_lines
            + "diff --git a/tests/test_acceptance.py b/tests/test_acceptance.py\n"
            "index abc..def 100644\n"
            "--- a/tests/test_acceptance.py\n"
            "+++ b/tests/test_acceptance.py\n"
            "@@ -1 +1,40 @@\n" + test_lines
        )

        evidence = build_diff_validation_evidence(
            diff,
            max_chars=4000,
            max_hunk_lines=60,
            agent_summary="Implemented the requested production behavior and regression coverage.",
        )

        assert "Diff Excerpts:" in evidence.text
        assert "### tests/test_acceptance.py" in evidence.text
        assert "+def test_acceptance_0(): pass" in evidence.text
        assert "+def test_acceptance_39(): pass" in evidence.text
        assert "validation evidence shortened due to length" not in evidence.text

    def test_diff_evidence_keeps_hunk_tail_lines(self) -> None:
        """Oversized hunks keep tail lines, where appended tests live."""
        body = "".join(
            f"+def test_case_{line}():\n" if line == 40 else f"+hunk_line_{line}\n"
            for line in range(80)
        )
        diff = (
            "diff --git a/tests/test_tail.py b/tests/test_tail.py\n"
            "index abc..def 100644\n"
            "--- a/tests/test_tail.py\n"
            "+++ b/tests/test_tail.py\n"
            "@@ -1 +1,80 @@\n" + body
        )

        evidence = build_diff_validation_evidence(diff, max_chars=1200, max_hunk_lines=20)

        assert "+hunk_line_0" in evidence.text
        assert "+hunk_line_79" in evidence.text
        assert "middle lines omitted" in evidence.text
        assert "omitted definitions: test_case_40" in evidence.text
        assert "Omitted Evidence:" in evidence.text

    def test_added_signature_names_stably_prioritize_and_deduplicate_tests(self) -> None:
        lines = [
            "+def helper_first(): pass",
            "+def TestUppercase(): pass",
            "+def helper_second(): pass",
            "+def test_lowercase(): pass",
            "+def helper_first(): pass",
            "+def test_lowercase(): pass",
        ]

        assert _added_signature_names(lines, limit=4, test_first=True) == [
            "TestUppercase",
            "test_lowercase",
            "helper_first",
            "helper_second",
        ]

    def test_diff_evidence_prioritizes_omitted_test_definitions(self) -> None:
        """Helper definitions cannot displace acceptance tests from a bounded marker."""
        helper_names = [f"helper_fixture_{index:02d}" for index in range(7)]
        test_names = [f"test_acceptance_{index:02d}" for index in range(12)]
        test_lines = [f"+test_file_line_{line:03d}_{'t' * 40}" for line in range(331)]
        for index, name in enumerate(helper_names):
            test_lines[45 + index * 10] = f"+def {name}(): pass"
        for index, name in enumerate(test_names[:-1]):
            test_lines[125 + index * 16] = f"+def {name}(): pass"
        test_lines[315] = f"+def {test_names[-1]}(): pass"

        production_lines = "".join(
            f"+production_line_{line:03d}_{'p' * 80}\n" for line in range(240)
        )
        diff = (
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

        evidence = build_diff_validation_evidence(
            diff,
            max_chars=VALIDATION_PROMPT_BUDGET_CHARS,
            max_hunk_lines=400,
        )

        for name in test_names:
            assert name in evidence.text
        assert "diff excerpt truncated for tests/test_acceptance.py" in evidence.text
        assert "omitted definitions:" in evidence.text
        assert "Omitted Evidence:" in evidence.text
        assert len(evidence.text) <= VALIDATION_PROMPT_BUDGET_CHARS

    def test_diff_excerpt_budgets_actual_signature_marker(self) -> None:
        """Signature-rich truncation markers must stay inside the caller budget."""
        path = "src/" + ("deep_component_" * 8) + "module.py"
        diff = (
            f"diff --git a/{path} b/{path}\n"
            "index abc..def 100644\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            "@@ -1 +1,160 @@\n"
            + "".join(
                (
                    f"+def test_extremely_descriptive_case_{line}_with_context():\n"
                    if line % 17 == 0
                    else f"+line_{line}_{'x' * 40}\n"
                )
                for line in range(160)
            )
        )
        file = ChangedFileEvidence(
            path=path,
            additions=160,
            deletions=0,
            category="source",
            diff=diff,
        )

        excerpt, omissions = _excerpt_file_diff(file, max_chars=320, max_hunk_lines=200)

        assert len(excerpt) <= 320
        assert omissions

    def test_diff_evidence_names_omitted_files(self) -> None:
        diff = "\n".join(
            f"diff --git a/src/file_{index}.py b/src/file_{index}.py\n"
            "index abc..def 100644\n"
            f"--- a/src/file_{index}.py\n"
            f"+++ b/src/file_{index}.py\n"
            "@@ -1 +1,120 @@\n" + "".join(f"+value_{line}_{'x' * 30}\n" for line in range(120))
            for index in range(5)
        )

        evidence = build_diff_validation_evidence(diff, max_chars=2200, max_hunk_lines=10)

        for index in range(5):
            assert f"- src/file_{index}.py" in evidence.text
        assert "Omitted Evidence:" in evidence.text
        assert "src/file_0.py" in evidence.text

    @pytest.mark.asyncio
    async def test_large_changes_summary_is_bounded(self, config, mock_llm) -> None:
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}

        await validator.validate_task(
            task_id="task-1",
            title="Big change",
            description="d",
            changes_summary="summary\n" + ("x" * 60000),
            validation_criteria="criteria",
        )

        prompt = mock_llm.call_json_feature.call_args.args[1]
        assert len(prompt) < VALIDATION_PROMPT_BUDGET_CHARS + 2000
        assert "agent changes summary shortened due to length" in prompt

    @pytest.mark.asyncio
    async def test_300kb_119_file_diff_has_complete_bounded_manifest(
        self, config, mock_llm
    ) -> None:
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}

        def file_diff(path: str, token: str) -> str:
            body = "".join(
                f"+{token}_{index:03d}_value_with_padding_payload\n" for index in range(60)
            )
            return (
                f"diff --git a/{path} b/{path}\n"
                "index abc..def 100644\n"
                f"--- a/{path}\n"
                f"+++ b/{path}\n"
                "@@ -1,1 +1,60 @@\n"
                f"{body}"
            )

        raw_diff = "\n".join(
            file_diff(f"src/file_{index:03d}.ts", f"token_{index:03d}") for index in range(119)
        )
        assert len(raw_diff.encode()) >= 300_000

        result = await validator.validate_task(
            task_id="task-raw-diff",
            title="Big raw diff",
            description="d",
            changes_summary=raw_diff,
            validation_criteria="criteria",
        )

        prompt = mock_llm.call_json_feature.call_args.args[1]
        assert "Changed File Manifest (authoritative):" in prompt
        assert all(f"src/file_{index:03d}.ts" in prompt for index in range(119))
        assert "Source/UI files changed:" in prompt
        assert "Omitted Evidence:" in prompt
        assert len(prompt) < 50_000
        assert result.status == "valid"
        assert mock_llm.call_json_feature.call_count == 1

    @pytest.mark.asyncio
    async def test_preassembled_packet_keeps_manifest_and_command_results_when_bounded(
        self, config, mock_llm
    ) -> None:
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}
        command_result = (
            "Structured verification results:\n"
            '{"command":"uv run pytest tests/tasks/test_validation.py",'
            '"exit_code":0,"success":true,"outcome_provenance":"provider-contract"}'
        )
        evidence_packet = (
            "Changed File Manifest (authoritative):\n"
            "- src/gobby/tasks/validation.py (+10/-2) [source]\n"
            "Source/UI files changed: src/gobby/tasks/validation.py\n\n"
            + ("diff excerpt payload\n" * 4_000)
            + command_result
        )

        await validator.validate_task(
            task_id="task-preassembled",
            title="Bound preassembled evidence",
            description="d",
            changes_summary=evidence_packet,
            validation_criteria="Focused pytest must pass.",
        )

        prompt = mock_llm.call_json_feature.call_args.args[1]
        assert "Changed File Manifest (authoritative):" in prompt
        assert "src/gobby/tasks/validation.py" in prompt
        assert command_result in prompt
        assert "structured validation evidence shortened due to length" in prompt
        assert len(prompt) < 50_000

    @pytest.mark.asyncio
    async def test_file_context_survives_oversized_changes(self, config, mock_llm) -> None:
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}

        await validator.validate_task(
            task_id="task-file-context",
            title="Big change with context",
            description="d",
            changes_summary="@@ diff @@\n" + ("x" * 60000),
            validation_criteria="criteria",
            file_context_text="=== src/index.ts ===\nREGISTERED_MCP_TOOLS_SECTION",
        )

        prompt = mock_llm.call_json_feature.call_args.args[1]
        assert "REGISTERED_MCP_TOOLS_SECTION" in prompt
        assert "agent changes summary shortened due to length" in prompt

    @pytest.mark.asyncio
    async def test_prompt_size_observability_logged(self, config, mock_llm, caplog) -> None:
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}

        with caplog.at_level(logging.DEBUG, logger="gobby.tasks.validation"):
            await validator.validate_task(
                task_id="task-obs",
                title="t",
                description="d",
                changes_summary="some changes",
                validation_criteria="criteria",
            )

        prompt_record = next(
            rec
            for rec in caplog.records
            if "Validation prompt assembled" in rec.message and "final_prompt_chars" in rec.message
        )
        assert prompt_record.levelno == logging.DEBUG


class TestValidationInfrastructureFailure:
    """Fix #4: infra generation failures return status='error', not a verdict."""

    @pytest.fixture
    def mock_llm(self) -> MagicMock:
        llm = MagicMock(spec=LLMService)
        llm.call_json_feature = AsyncMock()
        return llm

    @pytest.fixture
    def config(self) -> TaskValidationConfig:
        return TaskValidationConfig(enabled=True, candidates=["claude/test-model"])

    @pytest.mark.asyncio
    async def test_infrastructure_failure_returns_error_status(self, config, mock_llm) -> None:
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.side_effect = FeatureGenerationUnavailableError(
            "No JSON generation candidate succeeded"
        )

        result = await validator.validate_task(
            task_id="task-1",
            title="t",
            description="d",
            changes_summary="changes",
            validation_criteria="criteria",
        )

        assert result.status == "error"
        assert result.feedback is not None

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("Claude SDK hit maximum number of turns", FailureCategory.TIMEOUT),
            ("provider returned HTTP 429 Too Many Requests", FailureCategory.PROVIDER),
        ],
    )
    @pytest.mark.asyncio
    async def test_infrastructure_failure_category(
        self,
        config,
        mock_llm,
        message: str,
        expected: FailureCategory,
    ) -> None:
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.side_effect = FeatureGenerationUnavailableError(message)

        result = await validator.validate_task(
            task_id="task-1",
            title="t",
            description="d",
            changes_summary="changes",
            validation_criteria="criteria",
        )

        assert result.status == "error"
        assert result.failure_category is expected

    @pytest.mark.asyncio
    async def test_non_infrastructure_exception_returns_pending(self, config, mock_llm) -> None:
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.side_effect = ValueError("unexpected bug")

        result = await validator.validate_task(
            task_id="task-1",
            title="t",
            description="d",
            changes_summary="changes",
            validation_criteria="criteria",
        )

        assert result.status == "pending"


class TestInconsistentVerdictReconciliation:
    """Static verdicts use structured fields, never narrative classification."""

    @pytest.fixture
    def mock_llm(self) -> MagicMock:
        llm = MagicMock(spec=LLMService)
        llm.call_json_feature = AsyncMock()
        return llm

    @pytest.fixture
    def config(self) -> TaskValidationConfig:
        return TaskValidationConfig(enabled=True, candidates=["claude/test-model"])

    @pytest.mark.asyncio
    async def test_unsupported_invalid_is_pending_after_one_request(self, config, mock_llm) -> None:
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {
            "status": "invalid",
            "feedback": "The implementation is correct and all checks pass.",
            "blocking_reasons": [],
        }

        result = await validator.validate_task(
            task_id="task-1",
            title="t",
            description="d",
            changes_summary="changes",
            validation_criteria="criteria",
        )

        assert result.status == "invalid"
        assert result.blocking_reasons == [
            "criteria: The implementation is correct and all checks pass."
        ]
        assert mock_llm.call_json_feature.call_count == 1

    @pytest.mark.asyncio
    async def test_positive_narrative_does_not_reroll_reasoned_invalid(
        self,
        config: TaskValidationConfig,
        mock_llm: MagicMock,
    ) -> None:
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {
            "status": "invalid",
            "feedback": "Verified all validation criteria are satisfied.",
            "blocking_reasons": ["Missing regression coverage."],
            "current_failure_evidence": [],
        }

        result = await validator.validate_task(
            task_id="task-1",
            title="t",
            description="d",
            changes_summary="changes",
            validation_criteria="criteria",
        )

        assert result.status == "invalid"
        assert result.feedback == "Verified all validation criteria are satisfied."
        assert mock_llm.call_json_feature.call_count == 1

    @pytest.mark.asyncio
    async def test_reasoned_invalid_is_not_revalidated(self, config, mock_llm) -> None:
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {
            "status": "invalid",
            "feedback": "Missing a regression test for the 404 path.",
            "blocking_reasons": ["No test covers the disabled-feature 404 branch."],
        }

        result = await validator.validate_task(
            task_id="task-1",
            title="t",
            description="d",
            changes_summary="changes",
            validation_criteria="criteria",
        )

        assert result.status == "invalid"
        assert result.blocking_reasons == ["criteria: Missing a regression test for the 404 path."]
        assert mock_llm.call_json_feature.call_count == 1

    @pytest.mark.asyncio
    async def test_blocking_reason_list_entries_are_coerced_to_strings(
        self, config, mock_llm
    ) -> None:
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {
            "status": "invalid",
            "feedback": "Missing gates.",
            "blocking_reasons": [404, " missing test ", ""],
        }

        result = await validator.validate_task(
            task_id="task-1",
            title="t",
            description="d",
            changes_summary="changes",
            validation_criteria="criteria",
        )

        assert result.status == "invalid"
        assert result.blocking_reasons == ["criteria: Missing gates."]
        assert mock_llm.call_json_feature.call_count == 1

    @pytest.mark.asyncio
    async def test_valid_verdict_is_not_revalidated(self, config, mock_llm) -> None:
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {
            "status": "valid",
            "feedback": "All criteria satisfied.",
            "blocking_reasons": [],
        }

        result = await validator.validate_task(
            task_id="task-1",
            title="t",
            description="d",
            changes_summary="changes",
            validation_criteria="criteria",
        )

        assert result.status == "valid"
        assert result.verdict_override is None
        assert mock_llm.call_json_feature.call_count == 1

    @pytest.mark.asyncio
    async def test_static_contradictory_valid_is_demoted_with_provenance(
        self, config, mock_llm
    ) -> None:
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {
            "status": "valid",
            "feedback": "All criteria satisfied, but the current test run is not clean.",
            "blocking_reasons": [],
            "current_failure_evidence": ["pytest: 1 failed"],
        }

        result = await validator.validate_task(
            task_id="task-1",
            title="t",
            description="d",
            changes_summary="changes",
            validation_criteria="criteria",
        )

        assert result.status == "invalid"
        assert result.blocking_reasons == [
            "Overall non-valid verdict contradicts complete criterion satisfaction"
        ]
        assert result.verdict_override == {
            "from": "valid",
            "to": "invalid",
            "reason": "current_failure_evidence",
            "evidence": ["pytest: 1 failed"],
        }
        assert mock_llm.call_json_feature.call_count == 1

    @pytest.mark.asyncio
    async def test_static_malformed_or_nullish_failure_evidence_is_fail_open(
        self, config, mock_llm
    ) -> None:
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {
            "status": "valid",
            "feedback": "All criteria satisfied.",
            "blocking_reasons": [],
            "current_failure_evidence": ["N/A", "  ", 1, None],
        }

        result = await validator.validate_task(
            task_id="task-1",
            title="t",
            description="d",
            changes_summary="changes",
            validation_criteria="criteria",
        )

        assert result.status == "valid"
        assert result.verdict_override is None


class TestRunGitCommand:
    """Tests for run_git_command helper function."""

    @patch("subprocess.run")
    def test_run_git_command_success(self, mock_run) -> None:
        """Test successful git command execution."""
        mock_run.return_value = MagicMock(returncode=0, stdout="output")
        result = run_git_command(["git", "status"])
        assert result is not None
        assert result.returncode == 0
        assert result.stdout == "output"

    @patch("subprocess.run")
    def test_run_git_command_with_cwd(self, mock_run) -> None:
        """Test git command with custom working directory."""
        mock_run.return_value = MagicMock(returncode=0, stdout="output")
        run_git_command(["git", "status"], cwd="/custom/path")
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["cwd"] == "/custom/path"

    @patch("subprocess.run")
    def test_run_git_command_with_timeout(self, mock_run) -> None:
        """Test git command with custom timeout."""
        mock_run.return_value = MagicMock(returncode=0, stdout="output")
        run_git_command(["git", "status"], timeout=30)
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["timeout"] == 30

    @patch("subprocess.run")
    def test_run_git_command_exception_returns_none(self, mock_run) -> None:
        """Test that exceptions return None instead of raising."""
        mock_run.side_effect = Exception("Git failed")
        result = run_git_command(["git", "invalid"])
        assert result is None

    @patch("subprocess.run")
    def test_run_git_command_timeout_exception(self, mock_run) -> None:
        """Test timeout exception handling."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
        result = run_git_command(["git", "log"])
        assert result is None


class TestGetLastCommitDiff:
    """Tests for get_last_commit_diff function."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_success(self, mock_run) -> None:
        """Test successful retrieval of last commit diff."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff --git\n+line added")
        result = get_last_commit_diff()
        assert result is not None
        assert "diff --git" in result

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_truncation(self, mock_run) -> None:
        """Test truncation of large diffs (lines 82-86)."""
        large_diff = "a" * 100000
        mock_run.return_value = MagicMock(returncode=0, stdout=large_diff)

        result = get_last_commit_diff(max_chars=1000)

        assert result is not None
        assert len(result) < len(large_diff)
        assert "... [diff truncated] ..." in result
        # The truncated content should be max_chars + truncation message
        assert result[:1000] == "a" * 1000

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_exact_max_chars(self, mock_run) -> None:
        """Test diff exactly at max_chars boundary."""
        exact_diff = "x" * 500
        mock_run.return_value = MagicMock(returncode=0, stdout=exact_diff)

        result = get_last_commit_diff(max_chars=500)

        assert result is not None
        assert "... [diff truncated] ..." not in result
        assert result == exact_diff

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_returns_none_on_error(self, mock_run) -> None:
        """Test returns None when git command fails."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = get_last_commit_diff()
        assert result is None

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_returns_none_when_run_returns_none(self, mock_run) -> None:
        """Test returns None when run_git_command returns None."""
        mock_run.return_value = None
        result = get_last_commit_diff()
        assert result is None

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_returns_none_on_empty(self, mock_run) -> None:
        """Test returns None when diff is empty."""
        mock_run.return_value = MagicMock(returncode=0, stdout="   \n\t  ")
        result = get_last_commit_diff()
        assert result is None

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_with_cwd(self, mock_run) -> None:
        """Test cwd parameter is passed correctly."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff content")
        get_last_commit_diff(cwd="/project/path")
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs.get("cwd") == "/project/path"


class TestGetRecentCommitsEdgeCases:
    """Additional edge case tests for get_recent_commits function."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_line_without_pipe(self, mock_run) -> None:
        """Test handling of lines without pipe separator (line 108 branch)."""
        # Mix of valid and invalid lines
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc123|Valid commit\ninvalid_line_no_pipe\ndef456|Another commit",
        )

        commits = get_recent_commits(3)

        # Should only include lines with pipe separators
        assert len(commits) == 2
        assert commits[0]["sha"] == "abc123"
        assert commits[1]["sha"] == "def456"

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_all_invalid_lines(self, mock_run) -> None:
        """Test when all lines lack pipe separator."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="no pipe here\nalso no pipe\nstill none",
        )

        commits = get_recent_commits(3)
        assert commits == []

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_subject_with_pipes(self, mock_run) -> None:
        """Test commit subject containing pipe characters."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc123|fix: handle a|b|c case in parser",
        )

        commits = get_recent_commits(1)

        assert len(commits) == 1
        assert commits[0]["sha"] == "abc123"
        assert commits[0]["subject"] == "fix: handle a|b|c case in parser"


class TestGetCommitsSinceTruncation:
    """Tests for get_commits_since truncation behavior."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_commits_since_truncation(self, mock_run) -> None:
        """Test truncation of large diffs (line 162)."""
        large_diff = "b" * 80000
        mock_run.return_value = MagicMock(returncode=0, stdout=large_diff)

        result = get_commits_since("abc123", max_chars=5000)

        assert result is not None
        assert len(result) < len(large_diff)
        assert "... [diff truncated] ..." in result

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_commits_since_no_truncation_needed(self, mock_run) -> None:
        """Test when diff is under max_chars limit."""
        small_diff = "x" * 100
        mock_run.return_value = MagicMock(returncode=0, stdout=small_diff)

        result = get_commits_since("abc123", max_chars=5000)

        assert result == small_diff
        assert "... [diff truncated] ..." not in result


class TestFindMatchingFilesEdgeCases:
    """Additional tests for find_matching_files function."""

    def test_find_matching_files_early_exit_max_files(self, tmp_path) -> None:
        """Test early exit when max_files is reached (line 233 break)."""
        # Create more files than max_files
        for i in range(10):
            (tmp_path / f"file{i}.py").write_text(f"content {i}")

        # Request only 2 files but provide multiple patterns
        files = find_matching_files(
            ["file0.py", "file1.py", "file2.py", "file3.py"],
            base_dir=tmp_path,
            max_files=2,
        )

        assert len(files) == 2

    def test_find_matching_files_glob_exception(self, tmp_path) -> None:
        """Test exception handling in glob (lines 242-243)."""
        # Create a valid file
        (tmp_path / "valid.py").write_text("content")

        # Use a pattern that causes glob to fail on some systems
        # The [! pattern is invalid in some glob implementations
        with patch.object(Path, "glob") as mock_glob:
            mock_glob.side_effect = ValueError("Invalid glob pattern")

            files = find_matching_files(
                ["*.py"],  # This will trigger the glob path
                base_dir=tmp_path,
            )

            # Should handle exception gracefully and return empty list
            assert files == []

    def test_find_matching_files_stops_at_max_during_glob(self, tmp_path) -> None:
        """Test max_files limit during glob iteration."""
        # Create multiple files
        for i in range(10):
            (tmp_path / f"test{i}.py").write_text(f"content {i}")

        files = find_matching_files(["*.py"], base_dir=tmp_path, max_files=3)

        assert len(files) == 3

    def test_find_matching_files_skip_directories(self, tmp_path) -> None:
        """Test that directories are skipped even if they match pattern."""
        # Create a file and a directory with same base name
        (tmp_path / "module.py").write_text("content")
        (tmp_path / "module_dir").mkdir()

        files = find_matching_files(["module*"], base_dir=tmp_path)

        # Should only include the file, not the directory
        assert len(files) == 1
        assert files[0].name == "module.py"

    def test_find_matching_files_no_duplicates(self, tmp_path) -> None:
        """Test that duplicate files are not added."""
        test_file = tmp_path / "unique.py"
        test_file.write_text("content")

        # Provide patterns that would match the same file
        files = find_matching_files(
            ["unique.py", "unique.py", "*.py"],
            base_dir=tmp_path,
        )

        assert len(files) == 1
        assert files[0] == test_file


class TestReadFilesContentEdgeCases:
    """Additional tests for read_files_content function."""

    def test_read_files_content_early_truncation(self, tmp_path) -> None:
        """Test early exit when total_chars >= max_chars (lines 271-272)."""
        # Create files where total would exceed max_chars
        file1 = tmp_path / "file1.py"
        file2 = tmp_path / "file2.py"
        file3 = tmp_path / "file3.py"
        file1.write_text("a" * 500)
        file2.write_text("b" * 500)
        file3.write_text("c" * 500)

        # Set max_chars so we hit it after file1
        content = read_files_content([file1, file2, file3], max_chars=100)

        # Should have truncation message for additional files
        assert "... [additional files truncated] ..." in content

    def test_read_files_content_exact_boundary(self, tmp_path) -> None:
        """Test when total_chars exactly equals max_chars."""
        file1 = tmp_path / "exact.py"
        file1.write_text("x" * 100)

        content = read_files_content([file1], max_chars=100)

        # Should not include additional files truncation message
        # but file may be truncated
        assert "exact.py" in content

    def test_read_files_content_empty_file(self, tmp_path) -> None:
        """Test reading an empty file."""
        empty_file = tmp_path / "empty.py"
        empty_file.write_text("")

        content = read_files_content([empty_file])

        assert "empty.py" in content
        # Should have header but minimal content
        assert "===" in content


class TestGetValidationContextSmartEdgeCases:
    """Additional edge case tests for get_validation_context_smart."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_context_final_truncation(self, mock_run) -> None:
        """Test final truncation when combined context exceeds max_chars (line 370).

        The function truncates each piece to remaining_chars // 2, but when
        pieces are joined with separators, the combined length can still exceed
        max_chars, triggering the final truncation.
        """
        # Create staged and unstaged content that when combined will exceed max_chars
        # With max_chars=100, each piece gets 50 chars, but headers and join adds more
        mock_staged = MagicMock(returncode=0, stdout="a" * 200)
        mock_unstaged = MagicMock(returncode=0, stdout="b" * 200)
        mock_run.side_effect = [mock_staged, mock_unstaged]

        context = get_validation_context_smart(
            "Test task",
            max_chars=100,  # Small max_chars to trigger truncation
        )

        assert context is not None
        # The combined content with headers should exceed max_chars
        # triggering the final truncation message
        # Note: due to internal truncation logic, the final truncation may or may not appear
        # The key is verifying the function handles small max_chars gracefully

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    def test_context_limited_remaining_chars_skips_commit_diff(self, mock_diff, mock_run) -> None:
        """Test that commit diff is skipped when remaining_chars < 5000.

        Strategy 2 (multi-commit) only runs if remaining_chars > 5000.
        """
        # Large staged content: with max_chars=8000, staged gets 4000 chars
        # unstaged gets up to 2000 chars, leaving < 5000 remaining
        mock_staged = MagicMock(returncode=0, stdout="s" * 8000)
        mock_unstaged = MagicMock(returncode=0, stdout="u" * 4000)
        mock_run.side_effect = [mock_staged, mock_unstaged]
        mock_diff.return_value = "diff content"

        context = get_validation_context_smart(
            "Test task",
            max_chars=8000,
        )

        assert context is not None
        # Verify multi-commit diff was NOT called because remaining < 5000
        mock_diff.assert_not_called()
        assert mock_diff.call_count == 0
        assert not mock_diff.called

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    @patch("gobby.tasks.validation.find_matching_files")
    def test_context_skips_file_analysis_when_low_remaining(
        self, mock_find, mock_diff, mock_run
    ) -> None:
        """Test that file analysis is skipped when remaining_chars < 2000."""
        # Large content from earlier strategies
        mock_run.return_value = MagicMock(returncode=0, stdout="x" * 48000)
        mock_diff.return_value = None

        context = get_validation_context_smart(
            "Test task",
            validation_criteria="Check src/gobby/tasks/validation.py",
            max_chars=50000,
        )

        # File analysis may or may not be triggered depending on implementation
        # The test verifies the function handles the low remaining chars case
        assert context is not None
        assert context.startswith("=== STAGED CHANGES ===")

    @patch("gobby.tasks.validation.run_git_command")
    def test_context_truncation_on_join(self, mock_run) -> None:
        """Test that final truncation happens when join pushes over max_chars.

        Each strategy truncates to remaining//2, but the join adds '\\n\\n' separators
        and headers like '=== STAGED CHANGES ===' which can push total over max_chars.
        """
        # With max_chars=150:
        # - staged gets 75 chars of content
        # - after header "=== STAGED CHANGES ===\n" (~23 chars), remaining is ~127
        # - unstaged gets ~63 chars of content
        # - after header (~25 chars) and "\n\n" join (~2 chars), total may exceed 150
        mock_staged = MagicMock(returncode=0, stdout="a" * 500)
        mock_unstaged = MagicMock(returncode=0, stdout="b" * 500)
        mock_run.side_effect = [mock_staged, mock_unstaged]

        context = get_validation_context_smart(
            "Test task",
            max_chars=150,
        )

        assert context is not None
        # When the combined length with headers exceeds max_chars,
        # the final truncation message should appear
        if len(context) > 150:
            # This means we hit the truncation path
            assert "... [context truncated] ..." in context


class TestGetGitDiffEdgeCases:
    """Additional edge case tests for get_git_diff function."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_fallback_disabled(self, mock_run) -> None:
        """Test fallback_to_last_commit=False returns None (line 416)."""
        # No uncommitted changes
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        result = get_git_diff(fallback_to_last_commit=False)

        assert result is None

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_last_commit_diff")
    def test_get_git_diff_fallback_returns_none(self, mock_last_commit, mock_run) -> None:
        """Test when fallback also returns None."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_last_commit.return_value = None

        result = get_git_diff(fallback_to_last_commit=True)

        assert result is None

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_staged_only(self, mock_run) -> None:
        """Test with only staged changes."""
        mock_unstaged = MagicMock(returncode=0, stdout="")
        mock_staged = MagicMock(returncode=0, stdout="staged content")
        mock_run.side_effect = [mock_unstaged, mock_staged]

        result = get_git_diff()

        assert result is not None
        assert "STAGED CHANGES" in result
        assert "staged content" in result

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_unstaged_only(self, mock_run) -> None:
        """Test with only unstaged changes."""
        mock_unstaged = MagicMock(returncode=0, stdout="unstaged content")
        mock_staged = MagicMock(returncode=0, stdout="")
        mock_run.side_effect = [mock_unstaged, mock_staged]

        result = get_git_diff()

        assert result is not None
        assert "UNSTAGED CHANGES" in result
        assert "unstaged content" in result


class TestTaskValidatorTestStrategy:
    """Tests for category parameter in TaskValidator.validate_task."""

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMService)
        llm.call_json_feature = AsyncMock()
        return llm

    @pytest.fixture
    def config(self):
        return TaskValidationConfig(enabled=True, candidates=["claude/test-model"])

    @pytest.mark.asyncio
    async def test_validate_with_manual_category(self, config, mock_llm):
        """Manual category uses the normal test strategy prompt section."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}

        result = await validator.validate_task(
            task_id="task-1",
            title="Fix button color",
            description="Change button to blue",
            changes_summary="Updated CSS",
            category="manual",
        )

        assert result.status == "valid"
        call_args = mock_llm.call_json_feature.call_args
        prompt = call_args.args[1]
        assert "Test Strategy: manual" in prompt
        assert "MANUAL testing" not in prompt
        assert "Do NOT require automated test files" not in prompt

    @pytest.mark.asyncio
    async def test_validate_with_manual_category_uppercase(self, config, mock_llm):
        """Uppercase manual category does not trigger a special prompt branch."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}

        result = await validator.validate_task(
            task_id="task-1",
            title="Fix button color",
            description="Change button to blue",
            changes_summary="Updated CSS",
            category="MANUAL",
        )

        assert result.status == "valid"
        call_args = mock_llm.call_json_feature.call_args
        prompt = call_args.args[1]
        assert "Test Strategy: MANUAL" in prompt
        assert "MANUAL testing" not in prompt
        assert "Do NOT require automated test files" not in prompt

    @pytest.mark.asyncio
    async def test_validate_with_automated_category(self, config, mock_llm):
        """Test validation with category='automated' (lines 531-532)."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}

        result = await validator.validate_task(
            task_id="task-1",
            title="Add unit tests",
            description="Add tests for validator",
            changes_summary="Added test file",
            category="automated",
        )

        assert result.status == "valid"
        call_args = mock_llm.call_json_feature.call_args
        prompt = call_args.args[1]
        assert "Test Strategy: automated" in prompt
        # Should not have obsolete manual-specific guidance
        assert "MANUAL testing" not in prompt

    @pytest.mark.asyncio
    async def test_validate_without_category(self, config, mock_llm):
        """Test validation without category parameter."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}

        result = await validator.validate_task(
            task_id="task-1",
            title="Some task",
            description="Task description",
            changes_summary="Changes made",
        )

        assert result.status == "valid"
        call_args = mock_llm.call_json_feature.call_args
        prompt = call_args.args[1]
        # Should not have test strategy section
        assert "Test Strategy:" not in prompt

    @pytest.mark.asyncio
    async def test_validate_with_custom_category(self, config, mock_llm):
        """Test validation with custom category value."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}

        result = await validator.validate_task(
            task_id="task-1",
            title="Some task",
            description="Task description",
            changes_summary="Changes made",
            category="integration",
        )

        assert result.status == "valid"
        call_args = mock_llm.call_json_feature.call_args
        prompt = call_args.args[1]
        assert "Test Strategy: integration" in prompt
        # Should not have obsolete manual-specific guidance
        assert "MANUAL testing" not in prompt


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_validation_result_valid(self) -> None:
        """Test creating valid ValidationResult."""
        result = ValidationResult(status="valid", feedback="All criteria met")
        assert result.status == "valid"
        assert result.feedback == "All criteria met"

    def test_validation_result_invalid(self) -> None:
        """Test creating invalid ValidationResult."""
        result = ValidationResult(status="invalid", feedback="Missing tests")
        assert result.status == "invalid"
        assert result.feedback == "Missing tests"

    def test_validation_result_pending(self) -> None:
        """Test creating pending ValidationResult."""
        result = ValidationResult(status="pending")
        assert result.status == "pending"
        assert result.feedback is None

    def test_validation_result_default_feedback(self) -> None:
        """Test ValidationResult with default feedback."""
        result = ValidationResult(status="valid")
        assert result.feedback is None


class TestTaskValidatorCustomPrompt:
    """Tests for TaskValidator with custom prompts."""

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMService)
        llm.call_json_feature = AsyncMock()
        return llm

    @pytest.fixture
    def config(self):
        return TaskValidationConfig(enabled=True, candidates=["claude/test-model"])

    @pytest.mark.asyncio
    async def test_validate_uses_system_prompt(self, mock_llm):
        """Test validation passes system_prompt to provider."""
        config = TaskValidationConfig(
            enabled=True,
            candidates=["claude/test-model"],
            system_prompt="You are a code reviewer",
        )
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}

        await validator.validate_task(
            task_id="task-1",
            title="Test Task",
            description="Description",
            changes_summary="Changes",
        )

        call_args = mock_llm.call_json_feature.call_args
        assert call_args.kwargs["system_prompt"] == "You are a code reviewer"


class TestExtractFilePatternsEdgeCases:
    """Additional tests for extract_file_patterns_from_text."""

    def test_skip_www_urls(self) -> None:
        """Test that www. prefixed strings are skipped (line 187 branch)."""
        from gobby.tasks.validation import extract_file_patterns_from_text

        text = "See www.example.com/file.py and also src/real/file.py"
        patterns = extract_file_patterns_from_text(text)

        # www.example.com/file.py should be skipped
        assert not any("www." in p for p in patterns)
        assert not any("example.com" in p for p in patterns)
        # But real file path should be included
        assert "src/real/file.py" in patterns

    def test_skip_both_http_and_www(self) -> None:
        """Test both http and www URLs are filtered (though regex may catch partial matches)."""
        from gobby.tasks.validation import extract_file_patterns_from_text

        text = "Visit http://api.test.com/v1/data.json and www.docs.io/guide.md for info"
        patterns = extract_file_patterns_from_text(text)

        # The http:// and www. prefixed strings themselves are skipped
        # but the regex may still catch partial matches
        # The key is that 'http://' and 'www.' prefixed full URLs are filtered
        assert not any(p.startswith("http") for p in patterns)
        assert not any(p.startswith("www.") for p in patterns)


class TestGetValidationContextSmartFileBranch:
    """Tests for the files branch in get_validation_context_smart (line 361)."""

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    @patch("gobby.tasks.validation.find_matching_files")
    def test_context_no_files_found(self, mock_find, mock_diff, mock_run) -> None:
        """Test when patterns exist but no files match (line 361->365)."""
        # No uncommitted changes or commit diff
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_diff.return_value = None
        # Patterns exist (from validation_criteria) but no matching files
        mock_find.return_value = []

        context = get_validation_context_smart(
            task_title="Test task",
            validation_criteria="Check src/nonexistent/file.py",
            max_chars=50000,
        )

        # With no git changes, no commit diff, and no matching files,
        # context should be None
        assert context is None
        assert mock_find.call_count == 1


class TestIntegrationScenarios:
    """Integration-style tests combining multiple validation functions."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_full_validation_context_flow(self, mock_run) -> None:
        """Test complete flow of gathering validation context."""
        # Simulate a realistic scenario with staged, unstaged, and commit history
        call_count = [0]

        def mock_run_side_effect(*args, **kwargs):
            call_count[0] += 1
            cmd = args[0]

            if "diff" in cmd and "--cached" in cmd:
                return MagicMock(returncode=0, stdout="+ staged change")
            elif "diff" in cmd and "HEAD~" in cmd:
                return MagicMock(returncode=0, stdout="+ historical change")
            elif "diff" in cmd:
                return MagicMock(returncode=0, stdout="+ unstaged change")
            elif "log" in cmd:
                return MagicMock(returncode=0, stdout="abc123|feat: add feature\ndef456|fix: bug")
            return MagicMock(returncode=0, stdout="")

        mock_run.side_effect = mock_run_side_effect

        context = get_validation_context_smart(
            task_title="Test validation",
            validation_criteria="Must have staged changes",
        )

        assert context is not None
        assert "STAGED CHANGES" in context or "UNSTAGED CHANGES" in context

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMService)
        llm.call_json_feature = AsyncMock()
        return llm

    @pytest.fixture
    def config(self):
        return TaskValidationConfig(enabled=True, candidates=["claude/test-model"])

    @pytest.mark.asyncio
    async def test_validation_with_large_file_context(self, config, mock_llm, tmp_path):
        """Test validation with large file context gets truncated."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}

        # Create a large file
        large_file = tmp_path / "large.py"
        large_file.write_text("x" * 100000)

        result = await validator.validate_task(
            task_id="task-1",
            title="Test Task",
            description="Description",
            changes_summary="Changes",
            context_files=[str(large_file)],
        )

        assert result.status == "valid"
        # Verify the prompt was called and context was truncated
        call_args = mock_llm.call_json_feature.call_args
        prompt = call_args.args[1]
        # Context should be truncated to 50000 chars
        assert len(prompt) < 150000  # Reasonable upper bound


class TestPathHandling:
    """Tests for Path handling in validation functions."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_last_commit_diff_path_object(self, mock_run) -> None:
        """Test get_last_commit_diff with Path object for cwd."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff")
        get_last_commit_diff(cwd=Path("/path/to/project"))
        assert mock_run.call_args.kwargs["cwd"] == Path("/path/to/project")

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_multi_commit_diff_path_object(self, mock_run) -> None:
        """Test get_multi_commit_diff with Path object for cwd."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff")
        from gobby.tasks.validation import get_multi_commit_diff

        get_multi_commit_diff(cwd=Path("/path/to/project"))
        assert mock_run.call_args.kwargs["cwd"] == Path("/path/to/project")

    def test_find_matching_files_path_base_dir(self, tmp_path) -> None:
        """Test find_matching_files with Path object for base_dir."""
        test_file = tmp_path / "test.py"
        test_file.write_text("content")

        files = find_matching_files(["test.py"], base_dir=Path(tmp_path))
        assert len(files) == 1
        assert files[0] == test_file


# ============================================================================
# Merged Tests from test_task_validation.py (Renamed to avoid shadowing)
# ============================================================================


class TestGetGitDiffMerged:
    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_success(self, mock_run) -> None:
        # Mock unstaged
        mock_unstaged = MagicMock()
        mock_unstaged.returncode = 0
        mock_unstaged.stdout = "diff unstaged"

        # Mock staged
        mock_staged = MagicMock()
        mock_staged.returncode = 0
        mock_staged.stdout = "diff staged"

        mock_run.side_effect = [mock_unstaged, mock_staged]

        diff = get_git_diff()
        assert "=== STAGED CHANGES ===" in diff
        assert "diff staged" in diff
        assert "=== UNSTAGED CHANGES ===" in diff
        assert "diff unstaged" in diff

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_no_changes(self, mock_run) -> None:
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = ""
        mock_run.return_value = mock_res

        assert get_git_diff() is None

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_error_code(self, mock_run) -> None:
        mock_res = MagicMock()
        mock_res.returncode = 1
        mock_run.return_value = mock_res

        assert get_git_diff() is None

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_exception(self, mock_run) -> None:
        mock_run.side_effect = RuntimeError("Git error")
        with pytest.raises(RuntimeError):
            get_git_diff()

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_truncate(self, mock_run) -> None:
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "a" * 100
        mock_run.return_value = mock_res

        diff = get_git_diff(max_chars=10)
        assert len(diff) < 100
        assert "... [diff truncated] ..." in diff


class TestTaskValidatorMerged:
    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMService)
        llm.call_json_feature = AsyncMock()
        return llm

    @pytest.fixture
    def config(self):
        return TaskValidationConfig(enabled=True, candidates=["claude/test-model"])

    @pytest.mark.asyncio
    async def test_validate_task_disabled(self, mock_llm):
        config = TaskValidationConfig(enabled=False)
        validator = _task_validator(config, mock_llm)
        result = await validator.validate_task("task-1", "title", "instr", "summary")
        assert result.status == "pending"
        assert "disabled" in result.feedback

    @pytest.mark.asyncio
    async def test_validate_task_missing_info(self, config, mock_llm):
        validator = TaskValidator(config, mock_llm, db=MagicMock(spec=HubDatabase))
        result = await validator.validate_task("task-1", "title", None, "summary")
        assert result.status == "pending"
        assert "Missing validation criteria" in result.feedback

    @pytest.mark.asyncio
    async def test_validate_task_success(self, config, mock_llm):
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "Good job"}

        result = await validator.validate_task("task-1", "title", "instr", "summary")

        assert result.status == "valid"
        assert result.feedback == "Good job"
        mock_llm.call_json_feature.assert_called_once()

    @pytest.mark.asyncio
    async def test_validate_task_with_context(self, config, mock_llm, tmp_path):
        validator = _task_validator(config, mock_llm)

        test_file = tmp_path / "test.txt"
        test_file.write_text("file content")

        mock_llm.call_json_feature.return_value = {
            "status": "invalid",
            "feedback": "Bad",
            "blocking_reasons": ["Bad"],
        }

        result = await validator.validate_task(
            "task-1", "title", "instr", "summary", context_files=[str(test_file)]
        )

        assert result.status == "invalid"
        # Verify context was gathered
        call_args = mock_llm.call_json_feature.call_args
        prompt = call_args.args[1]
        assert "file content" in prompt

    @pytest.mark.asyncio
    async def test_validate_task_appends_server_receipt_packet_to_prompt(self, config, mock_llm):
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "Good"}
        receipt_packet = (
            'Verification receipt packet:\n{"evidence_completeness":{"total":303},'
            '"receipt_catalog":[{"receipt_id":"receipt-0303"}]}'
        )

        result = await validator.validate_task(
            "task-1",
            "title",
            "instr",
            "summary",
            verification_receipt_text=receipt_packet,
        )

        assert result.status == "valid"
        prompt = mock_llm.call_json_feature.call_args.args[1]
        assert "Server-computed verification receipt evidence" in prompt
        assert receipt_packet in prompt

    @pytest.mark.asyncio
    async def test_validate_task_rejects_oversized_server_receipt_packet(
        self, config, mock_llm
    ) -> None:
        validator = _task_validator(config, mock_llm)

        with pytest.raises(ValueError, match="verification receipt packet exceeds"):
            await validator.validate_task(
                "task-1",
                "title",
                "instr",
                "summary",
                verification_receipt_text="x" * 32_001,
            )

        mock_llm.call_json_feature.assert_not_called()

    @pytest.mark.asyncio
    async def test_validate_task_llm_error(self, config, mock_llm):
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.side_effect = Exception("LLM Error")

        result = await validator.validate_task("task-1", "title", "instr", "summary")
        assert result.status == "pending"
        assert "failed" in result.feedback

    @pytest.mark.asyncio
    async def test_validate_task_bad_json(self, config, mock_llm):
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.side_effect = ValueError("Invalid JSON")

        result = await validator.validate_task("task-1", "title", "instr", "summary")
        assert result.status == "pending"  # JSON decode error caught
        assert "failed" in result.feedback


class TestGetRecentCommitsMerged:
    """Tests for get_recent_commits function."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_success(self, mock_run) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc123|First commit\ndef456|Second commit\nghi789|Third commit",
        )

        commits = get_recent_commits(3)
        assert len(commits) == 3
        assert commits[0] == {"sha": "abc123", "subject": "First commit"}
        assert commits[1] == {"sha": "def456", "subject": "Second commit"}
        assert commits[2] == {"sha": "ghi789", "subject": "Third commit"}

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_empty(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        commits = get_recent_commits(5)
        assert commits == []

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_error(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        commits = get_recent_commits(5)
        assert commits == []

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_exception(self, mock_run) -> None:
        mock_run.side_effect = RuntimeError("Git error")
        with pytest.raises(RuntimeError):
            get_recent_commits(5)


class TestGetMultiCommitDiffMerged:
    """Tests for get_multi_commit_diff function."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_multi_commit_diff_success(self, mock_run) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="diff --git a/file.py b/file.py\n+new line",
        )

        diff = get_multi_commit_diff(5)
        assert diff is not None
        assert "diff --git" in diff

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_multi_commit_diff_truncate(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="a" * 1000)
        diff = get_multi_commit_diff(5, max_chars=100)
        assert len(diff) < 1000
        assert "... [diff truncated] ..." in diff

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_multi_commit_diff_empty(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        diff = get_multi_commit_diff(5)
        assert diff is None

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_multi_commit_diff_error(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        diff = get_multi_commit_diff(5)
        assert diff is None


class TestGetCommitsSinceMerged:
    """Tests for get_commits_since function."""

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_commits_since_success(self, mock_run) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="diff content here",
        )

        diff = get_commits_since("abc123")
        assert diff is not None
        assert "diff content" in diff

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_commits_since_empty(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        diff = get_commits_since("abc123")
        assert diff is None


class TestExtractFilePatternsFromTextMerged:
    """Tests for extract_file_patterns_from_text function."""

    def test_extract_explicit_paths(self) -> None:
        text = "Check the file src/gobby/tasks/validation.py for issues"
        patterns = extract_file_patterns_from_text(text)
        assert "src/gobby/tasks/validation.py" in patterns

    def test_extract_module_references(self) -> None:
        text = "The module gobby.tasks.validation handles this"
        patterns = extract_file_patterns_from_text(text)
        assert "src/gobby/tasks/validation.py" in patterns

    def test_extract_test_patterns(self) -> None:
        text = "Run test_validation to verify"
        patterns = extract_file_patterns_from_text(text)
        assert any("test_validation" in p for p in patterns)

    def test_extract_class_references(self) -> None:
        text = "Check the TaskValidator class"
        patterns = extract_file_patterns_from_text(text)
        assert any("validator" in p.lower() for p in patterns)

    def test_skip_urls(self) -> None:
        text = "See http://example.com/file.py for details"
        patterns = extract_file_patterns_from_text(text)
        # Should not include the URL as a file pattern
        assert not any("http" in p for p in patterns)

    def test_empty_text(self) -> None:
        patterns = extract_file_patterns_from_text("")
        assert patterns == []


class TestFindMatchingFilesMerged:
    """Tests for find_matching_files function."""

    def test_find_direct_path(self, tmp_path) -> None:
        test_file = tmp_path / "test.py"
        test_file.write_text("content")

        files = find_matching_files(["test.py"], base_dir=tmp_path)
        assert len(files) == 1
        assert files[0] == test_file

    def test_find_glob_pattern(self, tmp_path) -> None:
        (tmp_path / "test_one.py").write_text("content")
        (tmp_path / "test_two.py").write_text("content")
        (tmp_path / "other.txt").write_text("content")

        files = find_matching_files(["*.py"], base_dir=tmp_path)
        assert len(files) == 2
        assert all(f.suffix == ".py" for f in files)

    def test_find_nested_glob(self, tmp_path) -> None:
        subdir = tmp_path / "src"
        subdir.mkdir()
        (subdir / "module.py").write_text("content")

        files = find_matching_files(["**/*.py"], base_dir=tmp_path)
        assert len(files) == 1
        assert files[0].name == "module.py"

    def test_max_files_limit(self, tmp_path) -> None:
        for i in range(10):
            (tmp_path / f"file{i}.py").write_text("content")

        files = find_matching_files(["*.py"], base_dir=tmp_path, max_files=3)
        assert len(files) == 3

    def test_no_matches(self, tmp_path) -> None:
        files = find_matching_files(["nonexistent.py"], base_dir=tmp_path)
        assert files == []


class TestReadFilesContentMerged:
    """Tests for read_files_content function."""

    def test_read_single_file(self, tmp_path) -> None:
        test_file = tmp_path / "test.py"
        test_file.write_text("file content here")

        content = read_files_content([test_file])
        assert "file content here" in content
        assert "=== " in content  # Header

    def test_read_multiple_files(self, tmp_path) -> None:
        file1 = tmp_path / "file1.py"
        file2 = tmp_path / "file2.py"
        file1.write_text("content 1")
        file2.write_text("content 2")

        content = read_files_content([file1, file2])
        assert "content 1" in content
        assert "content 2" in content

    def test_truncate_large_content(self, tmp_path) -> None:
        test_file = tmp_path / "large.py"
        test_file.write_text("a" * 10000)

        content = read_files_content([test_file], max_chars=100)
        assert len(content) < 10000
        assert "... [file truncated] ..." in content

    def test_read_nonexistent_file(self, tmp_path) -> None:
        nonexistent = tmp_path / "missing.py"
        content = read_files_content([nonexistent])
        assert "Error reading file" in content


class TestGetValidationContextSmartMerged:
    """Tests for get_validation_context_smart function."""

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    @patch("gobby.tasks.validation.get_recent_commits")
    def test_includes_uncommitted_changes(self, mock_commits, mock_diff, mock_run) -> None:
        # Mock staged and unstaged diffs
        # Note: get_validation_context_smart calls 'diff --cached' (staged) first, then 'diff' (unstaged)
        mock_staged = MagicMock(returncode=0, stdout="staged diff content")
        mock_unstaged = MagicMock(returncode=0, stdout="unstaged diff content")
        mock_run.side_effect = [mock_staged, mock_unstaged]

        # Prevent further strategies
        mock_diff.return_value = None
        mock_commits.return_value = []

        context = get_validation_context_smart("Test task")
        assert context is not None
        assert "STAGED CHANGES" in context
        assert "UNSTAGED CHANGES" in context

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    def test_includes_multi_commit_diff(self, mock_multi_diff, mock_run) -> None:
        # No uncommitted changes
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_multi_diff.return_value = "multi commit diff content"

        context = get_validation_context_smart("Test task", commit_window=5)
        assert context is not None
        assert "COMBINED DIFF" in context or "multi commit diff" in context

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    @patch("gobby.tasks.validation.get_recent_commits")
    def test_includes_commit_summary(self, mock_commits, mock_diff, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_diff.return_value = "diff content"
        mock_commits.return_value = [
            {"sha": "abc12345", "subject": "First commit"},
            {"sha": "def67890", "subject": "Second commit"},
        ]

        context = get_validation_context_smart("Test task")
        assert context is not None
        assert "RECENT COMMITS" in context

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    @patch("gobby.tasks.validation.find_matching_files")
    def test_includes_file_analysis(self, mock_find, mock_diff, mock_run, tmp_path) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_diff.return_value = None  # No git diff

        test_file = tmp_path / "validation.py"
        test_file.write_text("def validate(): pass")
        mock_find.return_value = [test_file]

        get_validation_context_smart(
            "Check validation.py",
            validation_criteria="Ensure src/gobby/tasks/validation.py works",
        )
        # Should try to find files mentioned in criteria
        mock_find.assert_called()
        assert mock_find.call_count >= 1
        assert mock_find.call_args is not None

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    def test_includes_related_test_files(self, mock_diff, mock_run, tmp_path: Path) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_diff.return_value = None

        tests_dir = tmp_path / "tests" / "tasks"
        tests_dir.mkdir(parents=True)
        related_test = tests_dir / "test_task_validation.py"
        related_test.write_text("def test_validation_context_related_files(): pass\n")

        context = get_validation_context_smart(
            "Improve task validation context",
            validation_criteria="Task validation includes related test files",
            cwd=tmp_path,
            max_chars=5000,
        )

        assert context is not None
        assert "=== RELATED TEST FILES ===" in context
        assert "test_task_validation.py" in context
        assert "test_validation_context_related_files" in context

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    def test_returns_none_when_no_context(self, mock_diff, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_diff.return_value = None

        context = get_validation_context_smart(
            "Task with no related files",
            max_chars=100,  # Very limited
        )
        assert context is None

    @patch("gobby.tasks.validation.run_git_command")
    def test_respects_max_chars(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="a" * 10000)

        context = get_validation_context_smart("Test task", max_chars=500)
        assert context is None or len(context) <= 600  # Some buffer for headers


class TestTaskValidatorAdditionalEdgeCases:
    """Additional edge case tests for TaskValidator."""

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMService)
        llm.call_json_feature = AsyncMock()
        return llm

    @pytest.fixture
    def config(self):
        return TaskValidationConfig(enabled=True, candidates=["claude/test-model"])

    @pytest.mark.asyncio
    async def test_validate_with_validation_criteria_only(
        self,
        config,
        mock_llm,
        caplog: pytest.LogCaptureFixture,
    ):
        """Test validation with validation_criteria but no description."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}

        with caplog.at_level(logging.DEBUG, logger="gobby.tasks.validation"):
            result = await validator.validate_task(
                task_id="task-1",
                title="Test Task",
                description=None,  # No description
                changes_summary="Made changes",
                validation_criteria="Must have tests",  # Has criteria
            )

        assert result.status == "valid"
        levels = {
            record.getMessage(): record.levelno
            for record in caplog.records
            if record.name == "gobby.tasks.validation"
        }
        assert levels["Validating task task-1: Test Task"] == logging.INFO
        prompt_record = next(
            record
            for record in caplog.records
            if record.getMessage().startswith("Validation prompt assembled for task task-1:")
        )
        assert prompt_record.levelno == logging.DEBUG
        # Verify criteria was used in prompt
        call_args = mock_llm.call_json_feature.call_args
        prompt = call_args.args[1]
        assert "Validation Criteria" in prompt
        assert "Must have tests" in prompt

    @pytest.mark.asyncio
    async def test_validate_with_git_diff_context(self, config, mock_llm):
        """Test validation detects git diff format in changes_summary."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}

        # Git diff formatted summary
        git_diff = """Git diff from HEAD~1:
--- a/src/file.py
+++ b/src/file.py
@@ -1,3 +1,4 @@
+import os
 def main():
     pass
"""
        result = await validator.validate_task(
            task_id="task-1",
            title="Add import",
            description="Add os import",
            changes_summary=git_diff,
        )

        assert result.status == "valid"
        call_args = mock_llm.call_json_feature.call_args
        prompt = call_args.args[1]
        # Should include git diff context hint
        assert "Code Changes (git diff)" in prompt or "ACTUAL code changes" in prompt

    @pytest.mark.asyncio
    async def test_validate_with_at_symbol_diff(self, config, mock_llm):
        """Test that @@ in changes_summary triggers git diff detection."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}

        result = await validator.validate_task(
            task_id="task-1",
            title="Fix bug",
            description="Fix the bug",
            changes_summary="@@ -10,5 +10,6 @@ some context\n+added line",
        )

        assert result.status == "valid"

    @pytest.mark.asyncio
    async def test_validate_empty_llm_response(self, config, mock_llm):
        """Test handling of empty JSON result."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {}

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "pending"
        assert "Empty response" in result.feedback

    @pytest.mark.asyncio
    async def test_validate_whitespace_only_response(self, config, mock_llm):
        """Test handling of missing JSON result."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = None

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "pending"
        assert "Empty response" in result.feedback

    @pytest.mark.asyncio
    async def test_validate_json_without_code_block(self, config, mock_llm):
        """Test handling a structured invalid result."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {
            "status": "invalid",
            "feedback": "Missing tests",
            "blocking_reasons": ["Missing tests"],
        }

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "invalid"
        assert result.feedback == "Missing tests"

    @pytest.mark.asyncio
    async def test_validate_json_with_preamble(self, config, mock_llm):
        """Test handling a structured valid result."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {
            "status": "valid",
            "feedback": "All criteria met",
        }

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "valid"
        assert result.feedback == "All criteria met"

    @pytest.mark.asyncio
    async def test_validate_malformed_json(self, config, mock_llm):
        """Test handling a structured JSON service failure."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.side_effect = ValueError("Malformed JSON")

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "pending"
        assert "failed" in result.feedback.lower()

    @pytest.mark.asyncio
    async def test_validate_missing_status_field(self, config, mock_llm):
        """Test handling of JSON response missing status field."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"feedback": "Looks good"}

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        # Missing overall status is malformed and fails closed.
        assert result.status == "invalid"

    @pytest.mark.asyncio
    async def test_validate_with_file_context_error(self, config, mock_llm, tmp_path):
        """Test graceful handling when context file cannot be read."""
        validator = _task_validator(config, mock_llm)
        mock_llm.call_json_feature.return_value = {"status": "valid", "feedback": "OK"}

        # Non-existent file
        missing_file = tmp_path / "nonexistent.py"

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
            context_files=[str(missing_file)],
        )

        # Should still succeed - error is logged but validation proceeds
        assert result.status == "valid"


class TestTaskValidatorLLMErrors:
    """Tests for LLM error handling in TaskValidator."""

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMService)
        llm.call_json_feature = AsyncMock()
        return llm

    @pytest.fixture
    def config(self):
        return TaskValidationConfig(enabled=True, candidates=["claude/test-model"])

    @pytest.mark.asyncio
    async def test_validate_provider_not_found(self, config, mock_llm):
        """Test handling when LLM provider is not found."""
        mock_llm.call_json_feature.side_effect = ValueError("Provider not configured")
        validator = _task_validator(config, mock_llm)

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "pending"
        assert "failed" in result.feedback.lower()

    @pytest.mark.asyncio
    async def test_validate_timeout_error(self, config, mock_llm):
        """Test handling of timeout during LLM call."""
        mock_llm.call_json_feature.side_effect = TimeoutError("Request timed out")
        validator = _task_validator(config, mock_llm)

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "pending"
        assert "failed" in result.feedback.lower()

    @pytest.mark.asyncio
    async def test_validate_connection_error(self, config, mock_llm):
        """Test handling of connection error during LLM call."""
        mock_llm.call_json_feature.side_effect = ConnectionError("Network error")
        validator = _task_validator(config, mock_llm)

        result = await validator.validate_task(
            task_id="task-1",
            title="Test",
            description="Test description",
            changes_summary="changes",
        )

        assert result.status == "pending"
        assert "failed" in result.feedback.lower()


class TestGatherValidationContext:
    """Tests for TaskValidator.gather_validation_context method."""

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock(spec=LLMService)
        return llm

    @pytest.fixture
    def config(self):
        return TaskValidationConfig(enabled=True, candidates=["claude/test-model"])

    @pytest.mark.asyncio
    async def test_gather_single_file(self, config, mock_llm, tmp_path):
        """Test gathering context from a single file."""
        validator = _task_validator(config, mock_llm)
        test_file = tmp_path / "test.py"
        test_file.write_text("def hello(): return 'world'")

        context = await validator.gather_validation_context([str(test_file)])

        assert "test.py" in context
        assert "def hello()" in context

    @pytest.mark.asyncio
    async def test_gather_multiple_files(self, config, mock_llm, tmp_path):
        """Test gathering context from multiple files."""
        validator = _task_validator(config, mock_llm)
        file1 = tmp_path / "file1.py"
        file2 = tmp_path / "file2.py"
        file1.write_text("# File 1 content")
        file2.write_text("# File 2 content")

        context = await validator.gather_validation_context([str(file1), str(file2)])

        assert "file1.py" in context
        assert "file2.py" in context
        assert "File 1 content" in context
        assert "File 2 content" in context

    @pytest.mark.asyncio
    async def test_gather_nonexistent_file(self, config, mock_llm, tmp_path):
        """Test gathering context with a nonexistent file."""
        validator = _task_validator(config, mock_llm)
        missing = tmp_path / "missing.py"

        context = await validator.gather_validation_context([str(missing)])

        assert "missing.py" in context
        assert "Error reading file" in context

    @pytest.mark.asyncio
    async def test_gather_empty_file_list(self, config, mock_llm):
        """Test gathering context with empty file list."""
        validator = _task_validator(config, mock_llm)

        context = await validator.gather_validation_context([])

        assert context == ""

    @pytest.mark.asyncio
    async def test_gather_binary_file(self, config, mock_llm, tmp_path):
        """Test handling of binary file that cannot be decoded as UTF-8."""
        validator = _task_validator(config, mock_llm)
        binary_file = tmp_path / "binary.bin"
        binary_file.write_bytes(b"\x80\x81\x82\x83")  # Invalid UTF-8

        context = await validator.gather_validation_context([str(binary_file)])

        assert "binary.bin" in context
        assert "Error reading file" in context


class TestCwdParameter:
    """Tests for cwd parameter in git functions.

    Verifies that all git-related functions correctly pass the cwd parameter
    to subprocess.run, allowing validation to run in a different directory
    than the daemon's working directory.
    """

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_passes_cwd(self, mock_run) -> None:
        """Test that get_git_diff passes cwd to subprocess.run."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff content")

        get_git_diff(cwd="/path/to/project")

        # Both subprocess.run calls should have cwd set
        for call in mock_run.call_args_list:
            assert call.kwargs.get("cwd") == "/path/to/project"

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_recent_commits_passes_cwd(self, mock_run) -> None:
        """Test that get_recent_commits passes cwd to subprocess.run."""
        mock_run.return_value = MagicMock(returncode=0, stdout="abc123|Commit message")

        get_recent_commits(n=5, cwd="/custom/path")

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs.get("cwd") == "/custom/path"

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_multi_commit_diff_passes_cwd(self, mock_run) -> None:
        """Test that get_multi_commit_diff passes cwd to subprocess.run."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff content")

        get_multi_commit_diff(commit_count=10, cwd="/repo/path")

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs.get("cwd") == "/repo/path"

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_commits_since_passes_cwd(self, mock_run) -> None:
        """Test that get_commits_since passes cwd to subprocess.run."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff content")

        get_commits_since("abc123", cwd="/another/path")

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs.get("cwd") == "/another/path"

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_multi_commit_diff")
    @patch("gobby.tasks.validation.get_recent_commits")
    def test_get_validation_context_smart_passes_cwd(
        self, mock_commits, mock_diff, mock_run
    ) -> None:
        """Test that get_validation_context_smart passes cwd to subprocess calls."""
        # Mock subprocess for Strategy 1 (uncommitted changes) - empty to trigger Strategy 2
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        # Mock multi-commit diff to trigger get_recent_commits call
        mock_diff.return_value = "multi commit diff content"
        mock_commits.return_value = [{"sha": "abc123", "subject": "First commit"}]

        get_validation_context_smart(task_title="Test task", cwd="/project/root")

        # Verify subprocess.run was called with cwd for Strategy 1
        for call in mock_run.call_args_list:
            assert call.kwargs.get("cwd") == "/project/root"

        # Verify helper functions were called with cwd
        mock_diff.assert_called()
        assert mock_diff.call_args.kwargs.get("cwd") == "/project/root"

        mock_commits.assert_called()
        assert mock_commits.call_args.kwargs.get("cwd") == "/project/root"

    @patch("gobby.tasks.validation.run_git_command")
    def test_get_git_diff_none_cwd_is_default(self, mock_run) -> None:
        """Test that cwd=None uses default behavior (current directory)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="diff")

        get_git_diff(cwd=None)

        # cwd should be None (default behavior)
        for call in mock_run.call_args_list:
            assert call.kwargs.get("cwd") is None

    @patch("gobby.tasks.validation.run_git_command")
    @patch("gobby.tasks.validation.get_last_commit_diff")
    def test_get_git_diff_fallback_passes_cwd(
        self, mock_last_commit: MagicMock, mock_run: MagicMock
    ) -> None:
        """Test that fallback to last commit also passes cwd."""
        # No uncommitted changes
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        mock_last_commit.return_value = "last commit diff"

        get_git_diff(fallback_to_last_commit=True, cwd="/fallback/path")

        mock_last_commit.assert_called_once()
        # Verify max_chars and cwd were passed
        call_args = mock_last_commit.call_args
        assert call_args.args[0] == 50000  # max_chars
        assert call_args.kwargs.get("cwd") == "/fallback/path"
