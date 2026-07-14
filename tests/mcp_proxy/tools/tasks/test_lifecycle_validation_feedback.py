"""Regression tests for lifecycle validation feedback guards."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.tasks._lifecycle_validation import (
    feedback_admits_required_validation_failure,
    matched_required_validation_failure_pattern,
    matched_successful_validation_pattern,
    validate_leaf_task_with_llm,
)
from gobby.tasks.validation import ValidationResult as TaskValidationResult

pytestmark = pytest.mark.unit

_VALIDATION_FEEDBACK_17821 = (
    "The manifest and diff show a concrete root-cause fix: ensure_personal_project "
    "(src/gobby/storage/projects.py) now materializes .gobby/project.json with the canonical "
    "PERSONAL_PROJECT_ID for the personal workspace (write-if-missing, preserve-valid, "
    "repair-corrupt/wrong-id), which is exactly what daemon wiki routes need to avoid gwiki's "
    "invalid_scope failure for an uninitialized personal dir. This is covered by new focused "
    "tests in tests/storage/test_project_manager.py (identity materialization/preserve/repair) "
    "and a new daemon-route-seam test test_personal_scope_routes_resolve_uninitialized_workspace "
    "in tests/servers/routes/test_wiki_routes.py that provisions a bare gobby home and asserts "
    "/api/wiki/status, /pages, /graph all return ok:true for the personal scope, with a "
    "documented red-first failure against the pre-fix commit. A related second bug (vault-claim "
    "omission causing fallback-dir poisoning across five gwiki commands) was also fixed with a "
    "red-first Rust unit test in commands/health.rs described as covering the gwiki-side "
    "scope-resolution seam, backed by reported clean cargo test/clippy/fmt runs (826 passed). No "
    "frontend files changed per the manifest, consistent with the claim that the UI already "
    "degrades/renders correctly based on backend state; UI verification (headless run, "
    "screenshot, zero offline/degraded banner counts) supports criterion 3 without requiring "
    "source changes. Full raw diff for the five Rust files was omitted for length, but the "
    "manifest confirms the files/line-counts changed and the reported test evidence (specific "
    "failing line, specific passing counts, live e2e checks) is detailed and consistent with the "
    "narrative, so this omission isn't decision-blocking. All stated gates (mypy, ruff "
    "check/format, pytest 51 passed, cargo test/clippy/fmt) are reported clean with no "
    "contradicting or failing results."
)


class _NoBackoffConn:
    """Connection stub where validation backoff lookups always find no row."""

    rowcount = 0

    def execute(self, *_args: object, **_kwargs: object) -> _NoBackoffConn:
        return self

    def fetchone(self) -> None:
        return None


class _NoBackoffDB:
    """HubDatabase stub so TaskValidationBackoffStore reads as 'no active backoff'."""

    def fetchone(self, *_args: object, **_kwargs: object) -> None:
        return None

    @contextlib.contextmanager
    def transaction(self) -> Iterator[_NoBackoffConn]:
        yield _NoBackoffConn()


def _task_manager_mock(update_task: MagicMock) -> SimpleNamespace:
    return SimpleNamespace(update_task=update_task, db=_NoBackoffDB())


@pytest.mark.parametrize(
    "feedback",
    [
        "Required verification gate failed: Cargo test did not pass.",
        "CI check did not pass because the Go integration tests are still failing.",
        "Build errors remain in the TypeScript package.",
        "Compiler errors remain in the Rust crate.",
        "Static analysis check failed for the Java service.",
        "The acceptance criteria are not met because the UI workflow is incomplete.",
    ],
)
def test_feedback_admits_required_validation_failure_across_languages(feedback: str) -> None:
    """Required validation failures are detected without Python-specific tool names."""
    assert feedback_admits_required_validation_failure(feedback) is True


@pytest.mark.parametrize(
    ("feedback", "expected"),
    [
        ("Required validation\n\ncheck did not\npass after retry.", True),
        ("The criteria not satisfied by the delivered implementation.", True),
        ("Validation errors remain unresolved in the frontend package.", True),
        ("Mypy found incomplete type hints in the service boundary.", True),
        ("The script's validation gate failed.", True),
        ("The implementation mentions criteria and satisfied users.", False),
        ("Errors were documented and resolved before closure.", False),
        ("Mypy hints were improved and all work is complete.", False),
    ],
)
def test_multiline_and_specific_pattern_variants(feedback: str, expected: bool) -> None:
    """Failure feedback detection handles multiline positives without near-miss matches."""
    assert feedback_admits_required_validation_failure(feedback) is expected


def test_matched_pattern_helper_returns_the_triggering_pattern() -> None:
    """The override path can log the concrete pattern that matched."""
    feedback = "Required verification gate failed: cargo test failed."

    pattern = matched_required_validation_failure_pattern(feedback)

    assert pattern is not None
    assert pattern.search("verification gate failed") is not None
    assert feedback_admits_required_validation_failure(feedback) is True


@pytest.mark.parametrize("feedback", [None, "", "   ", "All checks pass."])
def test_matched_pattern_helper_returns_none_for_non_failures(feedback: str | None) -> None:
    """Empty or successful feedback does not report a matched pattern."""
    assert matched_required_validation_failure_pattern(feedback) is None


@pytest.mark.parametrize(
    "feedback",
    [
        "Not all acceptance criteria are met.",
        "not ALL validation criteria are satisfied.",
    ],
)
def test_successful_validation_pattern_ignores_not_all_feedback(feedback: str) -> None:
    assert matched_successful_validation_pattern(feedback) is None


@pytest.mark.parametrize(
    "feedback",
    [
        "Verified all validation criteria are satisfied.",
        "Verified all required validation criteria are satisfied.",
        "Resolved: all acceptance criteria were met.",
        "All validation criteria passed after the workflow was re-tested.",
        "All validation criteria are satisfied.",
        "All acceptance criteria are met.",
        "All acceptance criteria passed.",
        "Verified all criteria are satisfied.",
        # The #17636 incident rationale: an unambiguous approval phrased against
        # the task's own criteria list, returned alongside an invalid verdict.
        "All three criteria are addressed: (1) the slug identity fix landed, "
        "(2) regression tests cover the recompile, and (3) the binary was "
        "reinstalled, which is sufficient corroborating evidence.",
        "All 3 stated criteria are covered.",
    ],
)
def test_successful_validation_pattern_requires_explicit_verified_success(
    feedback: str,
) -> None:
    assert matched_successful_validation_pattern(feedback) is not None


@pytest.mark.parametrize(
    "feedback",
    [
        "Verified all previous validation criteria are satisfied.",
        "Resolved: all prior acceptance criteria were met.",
        "Verified all previously unmet validation criteria are satisfied.",
        "Re-tested: all unsatisfied validation criteria passed.",
    ],
)
def test_successful_validation_pattern_rejects_historical_or_mixed_criteria(
    feedback: str,
) -> None:
    assert matched_successful_validation_pattern(feedback) is None


@pytest.mark.parametrize(
    "feedback",
    [
        "All other criteria are met.",
        "All remaining criteria are satisfied.",
        "All three criteria are addressed except the coverage gate.",
        "All acceptance criteria are met, but the lint gate was not run.",
        "Not all three criteria are addressed.",
    ],
)
def test_successful_validation_pattern_rejects_partial_or_excepted_success(
    feedback: str,
) -> None:
    """Approval that implies an exception elsewhere never counts as success."""
    assert matched_successful_validation_pattern(feedback) is None


@pytest.mark.parametrize(
    "feedback",
    [
        "Tests were added for the new behavior.",
        "The build configuration was updated and validation can run locally.",
        "Static analysis coverage was expanded.",
    ],
)
def test_feedback_without_failure_admission_is_allowed(feedback: str) -> None:
    """Mentioning validation concepts alone is not treated as an admitted failure."""
    assert feedback_admits_required_validation_failure(feedback) is False


def test_successful_guard_feedback_does_not_match_across_sentence_boundary() -> None:
    """Success feedback can describe fail-fast guard behavior without admitting failure."""
    feedback = (
        "All acceptance criteria are met. pre-push-test.sh resolves DATABASE_URL via a "
        "three-tier fallback (env -> bootstrap config -> docker-compose.test.yml) through "
        "resolve_pytest_database_url(), exports it into the isolated pytest environment as "
        'DATABASE_URL="$PYTEST_DATABASE_URL", and check_pytest_postgres_skip_guard() scans '
        "the pytest report for the Postgres DSN skip reason, failing the run clearly if "
        "found. Two new CI contract tests "
        "(test_pre_push_resolves_and_exports_postgres_database_url_for_pytest, "
        "test_pre_push_fails_if_postgres_skip_reason_reaches_pytest_report) structurally "
        "verify the script's resolution chain and skip guard. Verification evidence confirms "
        "ruff check/format clean and both test_postgres_test_stack.py and "
        "test_postgres_safety.py pass under GOBBY_TEST_PROTECT=1."
    )

    assert matched_required_validation_failure_pattern(feedback) is None
    assert feedback_admits_required_validation_failure(feedback) is False


def test_quoted_failure_examples_do_not_admit_failure() -> None:
    """Quoted examples from validation criteria are not treated as actual failures."""
    feedback = (
        "All acceptance criteria are met. Existing parametrized tests for same-sentence "
        "failures ('Tests are failing in the required check.', "
        "'The validation gate did not pass.') remain in place and still match."
    )

    assert matched_required_validation_failure_pattern(feedback) is None
    assert feedback_admits_required_validation_failure(feedback) is False


def test_api_validation_error_description_does_not_admit_failure() -> None:
    feedback = (
        "All acceptance criteria are satisfied. Non-audit API and MCP research requests "
        "now return a validation error without query, while audit requests remain queryless."
    )

    assert matched_successful_validation_pattern(feedback) is not None
    assert matched_required_validation_failure_pattern(feedback) is None
    assert feedback_admits_required_validation_failure(feedback) is False


def test_success_feedback_with_negated_missing_gates_does_not_admit_failure() -> None:
    feedback = (
        "All acceptance criteria are met. The Changed File Manifest confirms source, test, "
        "and config changes across all required areas. No missing gates or unmet criteria."
    )

    assert matched_successful_validation_pattern(feedback) is not None
    assert matched_required_validation_failure_pattern(feedback) is None
    assert feedback_admits_required_validation_failure(feedback) is False


@pytest.mark.parametrize(
    "feedback",
    [
        "tests: 10 passed, 0 failed",
        "Validation summary: zero failures and all checks passed.",
        "pytest report: failed=0, passed=18",
    ],
)
def test_zero_failure_summaries_do_not_admit_failure(feedback: str) -> None:
    """Benign zero-count failure tokens are ignored before failure-pattern matching."""
    assert matched_required_validation_failure_pattern(feedback) is None
    assert feedback_admits_required_validation_failure(feedback) is False


@pytest.mark.parametrize(
    "feedback",
    [
        "All acceptance criteria are met. The 5 previously failing tests now pass.",
        "Verified all validation criteria are satisfied; formerly failed checks are now green.",
        "All acceptance criteria are met. Prior failing tests have been fixed.",
    ],
)
def test_resolved_regression_summaries_do_not_admit_failure(feedback: str) -> None:
    """Resolved-regression wording is success context, not failure evidence."""
    assert matched_successful_validation_pattern(feedback) is not None
    assert matched_required_validation_failure_pattern(feedback) is None
    assert feedback_admits_required_validation_failure(feedback) is False


@pytest.mark.parametrize(
    "feedback",
    [
        # The #17754 incident: the validator echoed TDD red evidence and the
        # precedence guard demoted a 'valid' verdict to 'invalid' twice.
        "All acceptance criteria are met. TDD evidence is documented: red "
        "(3 failed - 404s and missing content-encoding), green (23 passed), "
        "refactor/final-green (59 passed), with exact pytest commands captured.",
        "All acceptance criteria are met. TDD evidence is documented: red "
        "(3 failing tests, 404s and missing content-encoding header), green "
        "(23 passed in test_wiki_routes.py), final-green (59 passed).",
        # The validation_criteria template phrase validators echo verbatim.
        "Red evidence: failing test output captured before implementation.",
        "TDD evidence includes 2 tests failing prior to implementation and a green run after.",
        "The red test run failed as expected; green and final-green runs pass.",
        "Red-first run: 1 FAILED with a hard failure, followed by green.",
        "The pre-fix run reported 1 failed; the post-fix run passed.",
    ],
)
def test_tdd_red_evidence_does_not_admit_failure(feedback: str) -> None:
    """TDD red-phase descriptions are expected failures, not admissions."""
    assert matched_required_validation_failure_pattern(feedback) is None
    assert feedback_admits_required_validation_failure(feedback) is False


def test_17821_positive_feedback_does_not_admit_failure() -> None:
    """The complete rejected #17821 verdict contains only historical failures."""
    assert matched_required_validation_failure_pattern(_VALIDATION_FEEDBACK_17821) is None
    assert feedback_admits_required_validation_failure(_VALIDATION_FEEDBACK_17821) is False


@pytest.mark.parametrize(
    "feedback",
    [
        "The reported test evidence (specific failing line, then passing counts) is complete.",
        "All stated gates are clean with no contradicting or failing results.",
        "There are no failing tests in the final run.",
        "The release proceeds without failed checks.",
        "Tests are not failing in the final run.",
    ],
)
def test_gate_failure_vocabulary_near_misses_do_not_admit_failure(feedback: str) -> None:
    """Gate nouns and failure words need a current-state predicate relationship."""
    assert matched_required_validation_failure_pattern(feedback) is None
    assert feedback_admits_required_validation_failure(feedback) is False


@pytest.mark.parametrize(
    "feedback",
    [
        "Red evidence was captured, but the final test run failed.",
        "Tests are still failing in the required check after the TDD cycle.",
    ],
)
def test_current_failures_near_tdd_wording_still_admit_failure(feedback: str) -> None:
    """Genuine admissions keep their failure evidence despite TDD context."""
    assert matched_required_validation_failure_pattern(feedback) is not None
    assert feedback_admits_required_validation_failure(feedback) is True


@pytest.mark.parametrize(
    "feedback",
    [
        # Exact fragment from the #15950 round-2 verdict that misfired: the
        # override flipped a 'valid' LLM verdict because "test ... failed"
        # matched inside a description of an assertion.
        (
            "The agent's summary names focused tests for timeout/ok:false/degraded/"
            "presync-failure plus an end-to-end test asserting a real CronRun records "
            "status=failed with error populated, which aligns with the visible source logic."
        ),
        (
            "The end-to-end test asserts the recorded cron run is marked failed "
            "with error populated."
        ),
        "The handler change coerces degraded gwiki results into failed cron outcomes.",
        "New tests verify the executor records the run as failed when the envelope is degraded.",
        "The history output records status: failed so backoff can engage.",
        (
            "The storage test injects a raise between the two state changes and verifies both "
            "updates roll back, then retries successfully to reach FAILED/CANCELLED — directly "
            "matching the acceptance criterion. All acceptance criteria are met with concrete "
            "evidence."
        ),
    ],
)
def test_failure_state_assertion_descriptions_do_not_admit_failure(feedback: str) -> None:
    """Describing assertions or designed behavior about failure states is not an admission."""
    assert matched_required_validation_failure_pattern(feedback) is None
    assert feedback_admits_required_validation_failure(feedback) is False


@pytest.mark.parametrize(
    "feedback",
    [
        # Exact fragment from the #17734 verdict that misfired twice: `failed`
        # names the refresh payload's result bucket, and "test" follows within
        # the same-sentence proximity window.
        (
            "The explicit source_ids branch was not touched by this diff, so it "
            "retains its prior behavior of pushing into failed; this is "
            "corroborated by the pre-existing test "
            "explicit_unsupported_and_missing_sources_fail_structurally still "
            "passing per the agent's report."
        ),
        (
            "A new bulk-path test was added asserting the source appears under "
            "skipped with the correct code, an empty failed array, and status "
            "unchanged."
        ),
        "The selection routes missing-replay records into the failed list only for explicit ids.",
        "The compaction groups the failed entries by code before the check runs.",
        # Exact fragments from the #17734 close verdict that misfired after the
        # first round of bucket strips: an empty-bucket assertion and a
        # hyphenated naming compound, each near a gate word.
        ("The new test asserts skipped[0].code is correct and failed is empty for the bulk path."),
        ("The pre-existing test (unchanged) still covers the explicit-id failed-path assertion."),
    ],
)
def test_failure_bucket_references_do_not_admit_failure(feedback: str) -> None:
    """Naming a `failed` result bucket or collection is not a failure admission."""
    assert matched_required_validation_failure_pattern(feedback) is None
    assert feedback_admits_required_validation_failure(feedback) is False


@pytest.mark.parametrize(
    "feedback",
    [
        # Exact fragment from the #17766 close verdict that misfired: ask-mode
        # domain vocabulary ("error+retry", "resolved/unresolved citation
        # behavior") sits between the gate word "tests" and the bare adjective
        # "unresolved", which the loose errors-remain window read as
        # "test ... errors ... unresolved".
        (
            "WikiAskMode.test.tsx (14 tests) covers extractive/synthesized "
            "flows, staged progress/cancel, error+retry, resolved/unresolved "
            "citation behavior with mode flip and search fallback, grounding "
            "callout presence/absence, and history CRUD."
        ),
        ("The error chip and the unresolved citation chip are covered by the same rendering test."),
        (
            "New tests verify error+retry rendering and mark unresolved "
            "citations with a search-vault fallback."
        ),
    ],
)
def test_ui_error_state_vocabulary_does_not_admit_failure(feedback: str) -> None:
    """Feature vocabulary about error/unresolved UI states is not an admission."""
    assert matched_required_validation_failure_pattern(feedback) is None
    assert feedback_admits_required_validation_failure(feedback) is False


@pytest.mark.parametrize(
    "feedback",
    [
        "Test errors are unresolved in the frontend package.",
        "Errors remain unresolved in the coverage check.",
        "The lint gate reports errors still remaining after the fix.",
    ],
)
def test_predicated_errors_remain_still_admits_failure(feedback: str) -> None:
    """Errors genuinely predicated as remaining/unresolved still block closure."""
    assert matched_required_validation_failure_pattern(feedback) is not None
    assert feedback_admits_required_validation_failure(feedback) is True


@pytest.mark.parametrize(
    "feedback",
    [
        "tests: 9 passed, 1 failed",
        "Validation summary: 2 failures remain.",
        "Tests are failing in the required check.",
        "Tests are still failing in the required check.",
        "The validation gate did not pass.",
        "The new tests verify the retry path, but the lint check failed.",
        "Test evidence is complete, but lint check failed.",
        "Gates are clean; however, CI is still failing.",
        "Two failed tests remain in the required suite.",
        "The build failed under load testing.",
    ],
)
def test_nonzero_and_explicit_failure_summaries_still_admit_failure(feedback: str) -> None:
    """Nonzero and explicit failure summaries still block lifecycle closure."""
    assert feedback_admits_required_validation_failure(feedback) is True


@pytest.mark.asyncio
async def test_valid_llm_result_with_failure_feedback_is_overridden_to_invalid() -> None:
    """A valid status cannot close when feedback admits a required validation failure."""
    update_task = MagicMock()
    task = SimpleNamespace(
        id="task-1",
        title="Task",
        description="Description",
        validation_criteria="Tests pass",
        category="code",
    )
    validator = SimpleNamespace(
        validate_task=AsyncMock(
            return_value=TaskValidationResult(
                status="valid",
                feedback="Required validation gate did not pass.",
            )
        )
    )
    ctx = SimpleNamespace(task_manager=_task_manager_mock(update_task))

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "diff context",
        None,
        ctx,
        "task-1",
        None,
    )

    assert result.can_close is False
    assert result.extra == {"validation_status": "invalid"}
    update_task.assert_called_once_with(
        "task-1",
        validation_status="invalid",
        validation_feedback="Required validation gate did not pass.",
    )


@pytest.mark.asyncio
async def test_conflicting_success_and_failure_feedback_prefers_failure() -> None:
    """Failure evidence wins when feedback also contains explicit success text."""
    update_task = MagicMock()
    task = SimpleNamespace(
        id="task-1",
        title="Task",
        description="Description",
        validation_criteria="Tests pass",
        category="code",
    )
    feedback = (
        "Required validation gate did not pass. Verified all validation criteria are satisfied."
    )
    validator = SimpleNamespace(
        validate_task=AsyncMock(
            return_value=TaskValidationResult(
                status="invalid",
                feedback=feedback,
            )
        )
    )
    ctx = SimpleNamespace(task_manager=_task_manager_mock(update_task))

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "diff context",
        None,
        ctx,
        "task-1",
        None,
    )

    assert result.can_close is False
    assert result.extra == {"validation_status": "invalid"}
    update_task.assert_called_once_with(
        "task-1",
        validation_status="invalid",
        validation_feedback=feedback,
    )


@pytest.mark.asyncio
async def test_invalid_result_with_blocking_reasons_preserves_close_metadata() -> None:
    """Invalid close-layer validation surfaces blocking reasons to the caller."""
    update_task = MagicMock()
    task = SimpleNamespace(
        id="task-1",
        title="Task",
        description="Description",
        validation_criteria="Tests pass",
        category="code",
    )
    blocking_reasons = ["pytest regression still fails", "lint gate failed"]
    validator = SimpleNamespace(
        validate_task=AsyncMock(
            return_value=TaskValidationResult(
                status="invalid",
                feedback="Validation did not pass.",
                blocking_reasons=blocking_reasons,
            )
        )
    )
    ctx = SimpleNamespace(task_manager=_task_manager_mock(update_task))

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "diff context",
        None,
        ctx,
        "task-1",
        None,
    )

    assert result.can_close is False
    assert result.message == (
        "Validation did not pass.\n"
        "Blocking reasons: pytest regression still fails; lint gate failed"
    )
    assert result.extra == {
        "validation_status": "invalid",
        "blocking_reasons": blocking_reasons,
    }


@pytest.mark.asyncio
async def test_valid_llm_result_with_zero_failure_feedback_remains_valid() -> None:
    """A valid status stays valid when feedback only reports zero failures."""
    update_task = MagicMock()
    task = SimpleNamespace(
        id="task-1",
        title="Task",
        description="Description",
        validation_criteria="Tests pass",
        category="code",
    )
    validator = SimpleNamespace(
        validate_task=AsyncMock(
            return_value=TaskValidationResult(
                status="valid",
                feedback="tests: 10 passed, 0 failed",
            )
        )
    )
    ctx = SimpleNamespace(task_manager=_task_manager_mock(update_task))

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "diff context",
        None,
        ctx,
        "task-1",
        None,
    )

    assert result.can_close is True
    update_task.assert_called_once_with(
        "task-1",
        validation_status="valid",
        validation_feedback="tests: 10 passed, 0 failed",
    )


@pytest.mark.asyncio
async def test_valid_llm_result_with_17821_feedback_remains_valid() -> None:
    """Positive #17821 feedback remains valid and persists unchanged."""
    update_task = MagicMock()
    task = SimpleNamespace(
        id="task-1",
        title="Task",
        description="Description",
        validation_criteria="Tests pass",
        category="code",
    )
    validator = SimpleNamespace(
        validate_task=AsyncMock(
            return_value=TaskValidationResult(
                status="valid",
                feedback=_VALIDATION_FEEDBACK_17821,
                blocking_reasons=[],
            )
        )
    )
    ctx = SimpleNamespace(task_manager=_task_manager_mock(update_task))

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "diff context",
        None,
        ctx,
        "task-1",
        None,
    )

    assert result.can_close is True
    update_task.assert_called_once_with(
        "task-1",
        validation_status="valid",
        validation_feedback=_VALIDATION_FEEDBACK_17821,
    )


@pytest.mark.asyncio
async def test_invalid_llm_result_with_verified_success_feedback_is_promoted() -> None:
    """Invalid status is corrected only for explicit verified criteria success feedback."""
    update_task = MagicMock()
    task = SimpleNamespace(
        id="task-1",
        title="Task",
        description="Description",
        validation_criteria="Tests pass",
        category="code",
    )
    validator = SimpleNamespace(
        validate_task=AsyncMock(
            return_value=TaskValidationResult(
                status="invalid",
                feedback="Verified all validation criteria are satisfied.",
            )
        )
    )
    ctx = SimpleNamespace(task_manager=_task_manager_mock(update_task))

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "diff context",
        None,
        ctx,
        "task-1",
        None,
    )

    assert result.can_close is True
    update_task.assert_called_once_with(
        "task-1",
        validation_status="valid",
        validation_feedback="Verified all validation criteria are satisfied.",
    )


@pytest.mark.asyncio
async def test_invalid_llm_result_with_negated_failure_success_feedback_is_promoted() -> None:
    """Negated failure terms do not block promotion of explicit success feedback."""
    update_task = MagicMock()
    task = SimpleNamespace(
        id="task-1",
        title="Task",
        description="Description",
        validation_criteria="Tests pass",
        category="code",
    )
    feedback = (
        "All acceptance criteria are met. The Changed File Manifest confirms source, test, "
        "and config changes across all required areas. No missing gates or unmet criteria."
    )
    validator = SimpleNamespace(
        validate_task=AsyncMock(
            return_value=TaskValidationResult(
                status="invalid",
                feedback=feedback,
            )
        )
    )
    ctx = SimpleNamespace(task_manager=_task_manager_mock(update_task))

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "diff context",
        None,
        ctx,
        "task-1",
        None,
    )

    assert result.can_close is True
    update_task.assert_called_once_with(
        "task-1",
        validation_status="valid",
        validation_feedback=feedback,
    )


@pytest.mark.asyncio
async def test_invalid_result_with_resolved_regression_success_feedback_is_promoted() -> None:
    """Retrospective fixed-regression wording does not block explicit success feedback."""
    update_task = MagicMock()
    task = SimpleNamespace(
        id="task-1",
        title="Task",
        description="Description",
        validation_criteria="Tests pass",
        category="code",
    )
    feedback = (
        "All acceptance criteria are met. The 5 previously failing tests now pass "
        "under GOBBY_TEST_PROTECT=1."
    )
    validator = SimpleNamespace(
        validate_task=AsyncMock(
            return_value=TaskValidationResult(
                status="invalid",
                feedback=feedback,
            )
        )
    )
    ctx = SimpleNamespace(task_manager=_task_manager_mock(update_task))

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "diff context",
        None,
        ctx,
        "task-1",
        None,
    )

    assert result.can_close is True
    update_task.assert_called_once_with(
        "task-1",
        validation_status="valid",
        validation_feedback=feedback,
    )


@pytest.mark.asyncio
async def test_pending_llm_result_with_success_feedback_is_not_promoted() -> None:
    """Pending/error statuses stay blocking even when feedback contains success text."""
    update_task = MagicMock()
    task = SimpleNamespace(
        id="task-1",
        title="Task",
        description="Description",
        validation_criteria="Tests pass",
        category="code",
    )
    validator = SimpleNamespace(
        validate_task=AsyncMock(
            return_value=TaskValidationResult(
                status="pending",
                feedback=(
                    "Validation failed: could not parse response. "
                    "Verified all validation criteria are satisfied."
                ),
            )
        )
    )
    ctx = SimpleNamespace(task_manager=_task_manager_mock(update_task))

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "diff context",
        None,
        ctx,
        "task-1",
        None,
    )

    assert result.can_close is False
    assert result.extra == {"validation_status": "pending"}
    update_task.assert_called_once_with(
        "task-1",
        validation_status="pending",
        validation_feedback=(
            "Validation failed: could not parse response. "
            "Verified all validation criteria are satisfied."
        ),
    )
