"""Focused tests for Rule-4 Found Work stop enforcement."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.config.validation_detection import (
    classify_validation_command,
    classify_validation_segments,
)
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.transcript_evidence import (
    EvidenceOutcome,
    TranscriptEvidence,
    TranscriptValidationRun,
    TranscriptValidationSegment,
)
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.found_work_gate import (
    FoundWorkStopAnalyzer,
    FoundWorkStopFacts,
    capture_rule4_handoff,
    capture_turn_prompt,
    is_permission_deferral_candidate,
    resolve_stop_validation_config,
    unresolved_validation_failures,
)
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _event(
    event_type: HookEventType,
    data: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
        metadata=metadata or {},
    )


def _run(
    order: int,
    outcome: str,
    command: str = "pytest tests/unit/test_widget.py",
    categories: tuple[str, ...] | None = None,
    output: str | None = None,
) -> TranscriptValidationRun:
    """Build a run the way the transcript recorder does: one segment per validation match."""
    now = datetime.now(UTC)
    segments = tuple(
        TranscriptValidationSegment(command=match.normalized_command, categories=match.categories)
        for match in classify_validation_segments(command)
    )
    if categories is None:
        categories = tuple(
            dict.fromkeys(category for segment in segments for category in segment.categories)
        ) or ("test",)
    return TranscriptValidationRun(
        session_id=SESSION_ID,
        source="claude",
        command=command,
        categories=categories,
        matcher_id="pytest",
        label="pytest",
        outcome=cast(EvidenceOutcome, outcome),
        started_at=now,
        completed_at=now,
        order=order,
        exit_code=0 if outcome == "success" else 1,
        output=output,
        validation_segments=segments,
    )


class _Config:
    def __init__(self) -> None:
        self.validation_detection = None
        self.validation = TaskValidationConfig()

    def get_gobby_tasks_config(self) -> SimpleNamespace:
        return SimpleNamespace(validation=self.validation)


class TestPermissionDeferralFastPath:
    @pytest.mark.parametrize(
        "message",
        [
            "The parser is broken. Should I fix it?",
            "Tests are failing. Would you like me to investigate?",
            "I found a regression. Do you want me to address it?",
        ],
    )
    def test_detects_permission_question_about_defect(self, message: str) -> None:
        assert is_permission_deferral_candidate(message)

    @pytest.mark.parametrize(
        "message",
        [
            "The parser is broken. I fixed it and tests pass.",
            "The parser is broken. Should I fix it? I will proceed.",
            "Would you like a new dashboard theme?",
            "Should I explain how this works?",
            "Fixed the failing test.",
        ],
    )
    def test_skips_non_shirk_shapes(self, message: str) -> None:
        assert not is_permission_deferral_candidate(message)

    def test_turn_prompt_capture_uses_normalized_prompt_text(self) -> None:
        variables: dict[str, Any] = {}
        capture_turn_prompt(
            _event(HookEventType.BEFORE_AGENT, {"prompt_text": "Only audit this code."}),
            variables,
        )
        assert variables["_current_user_prompt"] == "Only audit this code."
        assert variables["_rule4_owner_handoff_turn"] is False
        assert variables["_rule4_fix_commit_turn"] is False

        capture_turn_prompt(_event(HookEventType.BEFORE_AGENT), variables)
        assert variables["_current_user_prompt"] == ""

    def test_successful_owner_handoff_is_tracked_for_current_turn(self) -> None:
        variables: dict[str, Any] = {}
        capture_rule4_handoff(
            _event(
                HookEventType.AFTER_TOOL,
                {
                    "mcp_server": "gobby-agents",
                    "mcp_tool": "send_message",
                    "tool_output": {"success": True, "result": {"sent": True}},
                },
                {"is_failure": False},
            ),
            variables,
        )
        assert variables["_rule4_owner_handoff_turn"] is True


class TestPermissionDeferralConfirmation:
    @pytest.mark.asyncio
    async def test_llm_confirmed_shirk_blocks(self) -> None:
        llm = SimpleNamespace(
            call_json_feature=AsyncMock(return_value={"block": True, "reason": "shirk"})
        )
        analyzer = FoundWorkStopAnalyzer(
            llm_service_resolver=lambda: llm,
            config_resolver=_Config,
            session_manager=None,
            session_task_manager=None,
        )
        variables: dict[str, Any] = {"_current_user_prompt": "Implement the parser."}

        facts = await analyzer.analyze(
            event=_event(
                HookEventType.STOP,
                {"last_assistant_message": "The parser is broken. Should I fix it?"},
            ),
            session_id=SESSION_ID,
            variables=variables,
            project_path=None,
        )

        assert facts.shirk is True
        llm.call_json_feature.assert_awaited_once()

        repeated = await analyzer.analyze(
            event=_event(
                HookEventType.STOP,
                {"last_assistant_message": "The parser is broken. Should I fix it?"},
            ),
            session_id=SESSION_ID,
            variables=variables,
            project_path=None,
        )
        assert repeated.shirk is True
        llm.call_json_feature.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_can_confirm_user_reserved_decision(self) -> None:
        llm = SimpleNamespace(
            call_json_feature=AsyncMock(return_value={"block": False, "reason": "reserved"})
        )
        analyzer = FoundWorkStopAnalyzer(
            llm_service_resolver=lambda: llm,
            config_resolver=_Config,
            session_manager=None,
            session_task_manager=None,
        )

        facts = await analyzer.analyze(
            event=_event(
                HookEventType.STOP,
                {"last_assistant_message": "The old records are incorrect. Should I fix them?"},
            ),
            session_id=SESSION_ID,
            variables={"_current_user_prompt": "Decide whether historical records should change."},
            project_path=None,
        )

        assert facts.shirk is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("prompt", "message"),
        [
            ("Only audit this code.", "The parser is broken. Should I fix it?"),
            ("Do not modify files.", "Tests are failing. Should I fix them?"),
            ("Implement the parser.", "Records are incorrect. Should I delete them?"),
        ],
    )
    async def test_deterministic_exemptions_skip_llm(self, prompt: str, message: str) -> None:
        llm = SimpleNamespace(call_json_feature=AsyncMock())
        analyzer = FoundWorkStopAnalyzer(
            llm_service_resolver=lambda: llm,
            config_resolver=_Config,
            session_manager=None,
            session_task_manager=None,
        )

        facts = await analyzer.analyze(
            event=_event(HookEventType.STOP, {"last_assistant_message": message}),
            session_id=SESSION_ID,
            variables={"_current_user_prompt": prompt},
            project_path=None,
        )

        assert facts.shirk is False
        llm.call_json_feature.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "variables",
        [
            {"task_claimed": True},
            {"_rule4_fix_commit_turn": True},
            {"_rule4_owner_handoff_turn": True},
        ],
    )
    async def test_ladder_evidence_clears_candidate(self, variables: dict[str, Any]) -> None:
        llm = SimpleNamespace(call_json_feature=AsyncMock())
        analyzer = FoundWorkStopAnalyzer(
            llm_service_resolver=lambda: llm,
            config_resolver=_Config,
            session_manager=None,
            session_task_manager=None,
        )

        facts = await analyzer.analyze(
            event=_event(
                HookEventType.STOP,
                {"last_assistant_message": "The parser is broken. Should I fix it?"},
            ),
            session_id=SESSION_ID,
            variables=variables,
            project_path=None,
        )

        assert facts.shirk is False
        llm.call_json_feature.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", ["needs-decision", "clean-window"])
    async def test_same_session_labeled_deferral_clears_candidate(self, label: str) -> None:
        llm = SimpleNamespace(call_json_feature=AsyncMock())
        task_links = SimpleNamespace(
            get_session_tasks=lambda _session_id: [
                {
                    "task": SimpleNamespace(
                        labels=[label],
                        created_in_session_id=SESSION_ID,
                    )
                }
            ]
        )
        analyzer = FoundWorkStopAnalyzer(
            llm_service_resolver=lambda: llm,
            config_resolver=_Config,
            session_manager=None,
            session_task_manager=task_links,
        )

        facts = await analyzer.analyze(
            event=_event(
                HookEventType.STOP,
                {"last_assistant_message": "The parser is broken. Should I fix it?"},
            ),
            session_id=SESSION_ID,
            variables={},
            project_path=None,
        )

        assert facts.shirk is False
        llm.call_json_feature.assert_not_awaited()


class TestTerminalValidationFailures:
    def test_terminal_failure_remains(self) -> None:
        assert unresolved_validation_failures([_run(1, "failure")], owner_handoff=False)

    def test_red_green_and_fail_fix_pass_clear(self) -> None:
        runs = [_run(1, "failure"), _run(2, "success")]
        assert unresolved_validation_failures(runs, owner_handoff=False) == ()

    def test_broader_green_covers_focused_failure(self) -> None:
        runs = [
            _run(1, "failure", "pytest tests/unit/test_widget.py"),
            _run(2, "success", "pytest tests/unit"),
        ]
        assert unresolved_validation_failures(runs, owner_handoff=False) == ()

    def test_scoped_green_does_not_hide_broader_failure(self) -> None:
        runs = [
            _run(1, "failure", "pytest tests/unit"),
            _run(2, "success", "pytest tests/unit/test_widget.py"),
        ]
        assert unresolved_validation_failures(runs, owner_handoff=False) == (runs[0],)

    def test_owner_handoff_without_confinement_does_not_clear_failure(self) -> None:
        runs = [
            _run(1, "failure", "pytest tests/foreign"),
            _run(2, "success", "pytest tests/owned"),
        ]
        assert unresolved_validation_failures(runs, owner_handoff=True) == (runs[0],)

    def test_verified_foreign_failure_plus_scoped_green_and_handoff_clears(self) -> None:
        runs = [
            _run(
                1,
                "failure",
                "pytest tests/foreign",
                output="FAILED tests/foreign/test_problem.py::test_case",
            ),
            _run(2, "success", "pytest tests/owned"),
        ]
        assert (
            unresolved_validation_failures(
                runs,
                owner_handoff=True,
                foreign_paths={"tests/foreign/test_problem.py"},
            )
            == ()
        )

    def test_foreign_failure_requires_green_scope_away_from_foreign_paths(self) -> None:
        runs = [
            _run(
                1,
                "failure",
                "pytest tests/foreign",
                output="FAILED tests/foreign/test_problem.py::test_case",
            ),
            _run(2, "success", "pytest tests/foreign/test_other.py"),
        ]
        assert unresolved_validation_failures(
            runs,
            owner_handoff=True,
            foreign_paths={
                "tests/foreign/test_problem.py",
                "tests/foreign/test_other.py",
            },
        ) == (runs[0],)

    def test_unrelated_validation_category_does_not_clear(self) -> None:
        runs = [
            _run(1, "failure", categories=("test",)),
            _run(2, "success", command="ruff check src", categories=("lint",)),
        ]
        assert unresolved_validation_failures(runs, owner_handoff=True) == (runs[0],)

    def test_scratch_log_wrapper_is_covered_by_later_pytest_of_same_files(self) -> None:
        failed = (
            "mkdir -p /tmp/scratch && cd /Users/me/wt && "
            "uv run pytest tests/cli/test_uninstall.py tests/cli/test_status.py "
            "> /tmp/scratch/wt-11079-fix.log"
        )
        runs = [
            _run(1, "failure", failed),
            _run(
                2,
                "success",
                "uv run pytest tests/cli/test_uninstall.py tests/cli/test_status.py",
            ),
        ]
        assert unresolved_validation_failures(runs, owner_handoff=False) == ()

    def test_wrapper_cover_still_requires_every_failed_test_file(self) -> None:
        failed = (
            "cd /Users/me/wt && uv run pytest tests/cli/test_uninstall.py "
            "tests/cli/test_status.py > /tmp/scratch/out.log"
        )
        runs = [
            _run(1, "failure", failed),
            _run(2, "success", "uv run pytest tests/cli/test_uninstall.py"),
        ]
        assert unresolved_validation_failures(runs, owner_handoff=False) == (runs[0],)

    def test_stash_wrapped_pytest_failure_is_covered_by_later_pytest_of_same_file(self) -> None:
        """Cover targets come from the validation segment, never from git/shell segments."""
        failed = (
            'git stash push -m "authfix-tmp" src/gobby/servers/grant_auth.py '
            "src/gobby/servers/auth_service.py src/gobby/servers/middleware/auth.py -q\n"
            "GOBBY_TEST_PROTECT=1 uv run pytest "
            "tests/servers/test_auth_service.py::test_agent_capability_survives_ref -q\n"
            "git stash pop -q\n"
            "git status --short"
        )
        runs = [
            _run(1, "failure", failed),
            _run(
                2,
                "success",
                "GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/test_auth_service.py -q",
            ),
        ]
        assert unresolved_validation_failures(runs, owner_handoff=False) == ()

    def test_file_green_covers_node_id_failure_but_not_the_reverse(self) -> None:
        node_id = "pytest tests/unit/test_widget.py::test_case"
        whole_file = "pytest tests/unit/test_widget.py"
        assert (
            unresolved_validation_failures(
                [_run(1, "failure", node_id), _run(2, "success", whole_file)],
                owner_handoff=False,
            )
            == ()
        )
        runs = [_run(1, "failure", whole_file), _run(2, "success", node_id)]
        assert unresolved_validation_failures(runs, owner_handoff=False) == (runs[0],)

    def test_every_validation_segment_of_a_compound_failure_needs_cover(self) -> None:
        failed = "uv run pytest tests/unit/test_a.py && uv run pytest tests/unit/test_b.py"
        runs = [
            _run(1, "failure", failed),
            _run(2, "success", "uv run pytest tests/unit/test_a.py"),
        ]
        assert unresolved_validation_failures(runs, owner_handoff=False) == (runs[0],)
        runs.append(_run(3, "success", "uv run pytest tests/unit"))
        assert unresolved_validation_failures(runs, owner_handoff=False) == ()

    def test_format_first_compound_green_covers_pytest_failure(self) -> None:
        """A green whose first segment is ruff still covers through its pytest segment."""
        runs = [
            _run(1, "failure", "uv run pytest tests/unit/test_a.py tests/unit/test_missing.py"),
            _run(
                2,
                "success",
                "uv run ruff format --check src/gobby/x.py && uv run pytest tests/unit -q",
            ),
        ]
        assert runs[1].categories == ("format", "test")
        assert unresolved_validation_failures(runs, owner_handoff=False) == ()

    def test_lint_segment_scope_does_not_cover_pytest_failure(self) -> None:
        """Only a segment of the failure's category can cover it, whatever paths it names."""
        runs = [
            _run(1, "failure", "uv run pytest tests/unit/test_a.py"),
            _run(
                2, "success", "uv run ruff check tests/unit && uv run pytest tests/other/test_b.py"
            ),
        ]
        assert unresolved_validation_failures(runs, owner_handoff=False) == (runs[0],)

    def test_selector_on_sibling_segment_does_not_narrow_pytest_segment(self) -> None:
        runs = [
            _run(1, "failure", "uv run pytest tests/unit/test_a.py"),
            _run(2, "success", "cargo test -p gobby-core && uv run pytest tests/unit"),
        ]
        assert unresolved_validation_failures(runs, owner_handoff=False) == ()

    def test_selector_narrowed_green_does_not_hide_broader_failure(self) -> None:
        runs = [
            _run(1, "failure", "pytest tests/unit/test_widget.py"),
            _run(2, "success", "pytest tests/unit/test_widget.py -k widget_a"),
        ]
        assert unresolved_validation_failures(runs, owner_handoff=False) == (runs[0],)

    def test_unscoped_green_covers_selector_narrowed_failure(self) -> None:
        runs = [
            _run(1, "failure", "pytest tests/unit/test_widget.py -k widget_a"),
            _run(2, "success", "pytest tests/unit/test_widget.py"),
        ]
        assert unresolved_validation_failures(runs, owner_handoff=False) == ()

    def test_identical_selector_green_covers_selector_narrowed_failure(self) -> None:
        runs = [
            _run(1, "failure", "pytest tests/unit/test_widget.py -k widget_a"),
            _run(2, "success", "pytest tests/unit/test_widget.py -k widget_a"),
        ]
        assert unresolved_validation_failures(runs, owner_handoff=False) == ()

    def test_project_verification_command_extends_detection(self, tmp_path: Path) -> None:
        project_dir = tmp_path / ".gobby"
        project_dir.mkdir()
        (project_dir / "project.json").write_text(
            '{"verification":{"custom":{"schema":"acme verify schema"}}}',
            encoding="utf-8",
        )

        config = resolve_stop_validation_config(
            daemon_config=_Config(),
            project_path=str(tmp_path),
        )

        match = classify_validation_command("acme verify schema", config)
        assert match is not None
        assert match.matcher_id == "project-verification-schema"

    @pytest.mark.asyncio
    async def test_analyzer_surfaces_transcript_terminal_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        derive = AsyncMock(return_value=TranscriptEvidence(validation_runs=(_run(1, "failure"),)))
        monkeypatch.setattr("gobby.workflows.found_work_gate.derive_transcript_evidence", derive)
        session = SimpleNamespace(created_at=datetime.now(UTC))
        analyzer = FoundWorkStopAnalyzer(
            llm_service_resolver=lambda: None,
            config_resolver=_Config,
            session_manager=SimpleNamespace(get=lambda _session_id: session),
            session_task_manager=None,
        )

        facts = await analyzer.analyze(
            event=_event(HookEventType.STOP),
            session_id=SESSION_ID,
            variables={"_rule4_owner_handoff_turn": True},
            project_path=str(tmp_path),
        )

        assert facts.terminal_validation_failures == ("pytest tests/unit/test_widget.py",)
        derive.assert_awaited_once()


class TestFoundWorkDeclarativeRules:
    @pytest.fixture(autouse=True)
    def sync_rules(self, temp_db: HubDatabase) -> None:
        sync_bundled_rules(temp_db, get_bundled_rules_path())

    @pytest.mark.asyncio
    async def test_permission_deferral_fact_blocks_with_ladder(self, temp_db: HubDatabase) -> None:
        response = await RuleEngine(temp_db).evaluate(
            _event(HookEventType.STOP),
            session_id=SESSION_ID,
            variables={},
            eval_context={"found_work_shirk": True},
        )

        assert response.decision == "block"
        assert "Rule 4 ladder" in (response.reason or "")
        assert "send_message" in (response.reason or "")
        assert "needs-decision/clean-window" in (response.reason or "")

    @pytest.mark.asyncio
    async def test_workflow_handler_feeds_analyzer_facts_to_rules(
        self,
        temp_db: HubDatabase,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handler = WorkflowHookHandler(rule_engine=RuleEngine(temp_db))
        handler._session_var_manager = None
        analyze = AsyncMock(return_value=FoundWorkStopFacts(shirk=True))
        monkeypatch.setattr(handler._found_work_analyzer, "analyze", analyze)
        event = _event(
            HookEventType.STOP,
            metadata={"_platform_session_id": SESSION_ID},
        )
        event.cwd = str(Path(__file__).resolve().parents[2])

        response = await handler.evaluate_async(event)

        assert response.decision == "block"
        assert "Rule 4 ladder" in (response.reason or "")
        analyze.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_terminal_failure_fact_blocks_with_command(self, temp_db: HubDatabase) -> None:
        response = await RuleEngine(temp_db).evaluate(
            _event(HookEventType.STOP),
            session_id=SESSION_ID,
            variables={},
            eval_context={
                "terminal_validation_failure": True,
                "terminal_validation_failure_commands": ["pytest tests/unit"],
            },
        )

        assert response.decision == "block"
        assert "pytest tests/unit" in (response.reason or "")

    @pytest.mark.asyncio
    async def test_no_detector_facts_allows_stop(self, temp_db: HubDatabase) -> None:
        response = await RuleEngine(temp_db).evaluate(
            _event(HookEventType.STOP),
            session_id=SESSION_ID,
            variables={},
            eval_context={
                "found_work_shirk": False,
                "terminal_validation_failure": False,
            },
        )

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_successful_close_injects_one_complete_reminder(
        self,
        temp_db: HubDatabase,
    ) -> None:
        response = await RuleEngine(temp_db).evaluate(
            _event(
                HookEventType.AFTER_TOOL,
                {
                    "tool_name": "mcp__gobby__call_tool",
                    "mcp_server": "gobby-tasks",
                    "mcp_tool": "close_task",
                    "tool_input": {"arguments": {"task_id": "#42"}},
                    "tool_output": {"success": True, "closed": True},
                },
                {"is_failure": False},
            ),
            session_id=SESSION_ID,
            variables={},
        )

        context = response.context or ""
        assert context.count("Task closed. Rule 4 sweep") == 1
        assert "new claimed task" in context
        assert "send_message" in context
        assert "needs-decision/clean-window" in context

    @pytest.mark.asyncio
    async def test_failed_close_injects_no_reminder(self, temp_db: HubDatabase) -> None:
        response = await RuleEngine(temp_db).evaluate(
            _event(
                HookEventType.AFTER_TOOL,
                {
                    "tool_name": "mcp__gobby__call_tool",
                    "mcp_server": "gobby-tasks",
                    "mcp_tool": "close_task",
                    "tool_input": {"arguments": {"task_id": "#42"}},
                    "tool_output": {"success": False, "error": "blocked"},
                },
                {"is_failure": True},
            ),
            session_id=SESSION_ID,
            variables={},
        )

        assert "Rule 4 sweep" not in (response.context or "")

    def test_bundled_template_has_no_repo_specific_commands(self) -> None:
        template = (
            get_bundled_rules_path() / "stop-gates" / "enforce-found-work-ladder.yaml"
        ).read_text(encoding="utf-8")
        for command in ("pytest", "ruff", "mypy", "cargo", "npm", "uv run"):
            assert command not in template
