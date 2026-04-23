"""Tests for task-enforcement.yaml rules.

Verifies blocking rules for native task tools, edit gating, commit
requirements, validation bypass prevention, stop compliance, and
task claim/release tracking.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_task_enforcement.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _sync_bundled(db):
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    # Mark templates as installed so get_by_name() finds them
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")
    return result


TASK_ENFORCEMENT_RULES = {
    "block-native-task-tools-unclaimed",
    "block-native-todo-write",
    "block-reopen-task",
    "block-front-half-on-interactive-lock",
    "inject-task-creation-on-schema",
    "inject-transition-skill",
    "require-task-creation-skill-loaded",
    "require-task-transitions-skill-loaded",
    "require-task-before-edit",
    "require-commit-before-status",
    "require-clean-tree-before-status",
    "strip-skip-validation-with-commit",
    "block-ask-during-stop-compliance",
    "track-task-claim",
    "reset-subagent-flag",
}


class TestTaskEnforcementSync:
    """Test that task-enforcement.yaml syncs correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All task-enforcement rules should sync to workflow_definitions."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        rule_names = {r.name for r in rules}

        assert TASK_ENFORCEMENT_RULES.issubset(rule_names), (
            f"Missing: {TASK_ENFORCEMENT_RULES - rule_names}"
        )

    def test_all_rules_have_group(self, db, manager) -> None:
        """All rules should have group='task-enforcement'."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in TASK_ENFORCEMENT_RULES:
                body = json.loads(row.definition_json)
                assert body.get("group") == "task-enforcement", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in TASK_ENFORCEMENT_RULES:
                body = RuleDefinitionBody.model_validate_json(row.definition_json)
                effect_types = {e.type for e in body.resolved_effects}
                assert effect_types <= {
                    "block",
                    "set_variable",
                    "observe",
                    "inject_context",
                    "mcp_call",
                    "rewrite_input",
                }


class TestBlockNativeTaskToolsUnclaimed:
    """Verify block-native-task-tools-unclaimed blocks task tools without a Gobby task."""

    def test_blocks_task_tools_when_unclaimed(self, db, manager) -> None:
        """Should block TaskCreate, TaskUpdate, TaskGet, TaskList."""
        _sync_bundled(db)

        row = manager.get_by_name("block-native-task-tools-unclaimed")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"

        expected_tools = {"TaskCreate", "TaskUpdate", "TaskGet", "TaskList"}
        assert set(body.effects[0].tools) == expected_tools

    def test_when_checks_is_subagent_and_task_claimed(self, db, manager) -> None:
        """Should only block when not subagent AND no task claimed."""
        _sync_bundled(db)

        row = manager.get_by_name("block-native-task-tools-unclaimed")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "is_subagent" in body.when
        assert "task_claimed" in body.when


class TestBlockNativeTodoWrite:
    """Verify block-native-todo-write blocks TodoWrite unconditionally."""

    def test_blocks_todo_write(self, db, manager) -> None:
        """Should block TodoWrite."""
        _sync_bundled(db)

        row = manager.get_by_name("block-native-todo-write")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"
        assert set(body.effects[0].tools) == {"TodoWrite"}

    def test_when_only_checks_is_subagent(self, db, manager) -> None:
        """Should block for all non-subagent sessions regardless of task_claimed."""
        _sync_bundled(db)

        row = manager.get_by_name("block-native-todo-write")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "is_subagent" in body.when
        assert "task_claimed" not in body.when


class TestRequireTaskBeforeEdit:
    """Verify require-task-before-edit blocks edits without claimed task."""

    def test_block_effect_is_not_tied_to_native_tool_names(self, db, manager) -> None:
        """The block effect should rely on canonical mutation semantics."""
        _sync_bundled(db)

        row = manager.get_by_name("require-task-before-edit")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"
        assert body.effects[0].tools in (None, [])

    def test_when_checks_task_claimed_mutation_and_plan_mode(self, db, manager) -> None:
        """Should check task_claimed and multi-file task gating."""
        _sync_bundled(db)

        row = manager.get_by_name("require-task-before-edit")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "task_claimed" in body.when
        assert "canonical_repo_mutation" in body.when
        assert "requires_task_for_any_touched_file" in body.when
        assert "plan_mode" in body.when

    def test_when_condition_evaluates_with_plan_file(self) -> None:
        """Plan files should stay exempt when the helper is registered."""
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

        condition = (
            "variables.get('require_task_before_edit') and not variables.get('task_claimed') "
            "and event.data.get('canonical_repo_mutation') "
            "and requires_task_for_any_touched_file(tool_input, source, variables.get('plan_mode'))"
        )

        # Scenario: editing a plan file without task claimed => should NOT block
        context = {
            "variables": {
                "require_task_before_edit": True,
                "task_claimed": False,
                "plan_mode": False,
            },
            "event": type("Event", (), {"data": {"canonical_repo_mutation": True}})(),
            "tool_input": {"file_path": "/project/.gobby/plans/my-plan.md"},
            "source": "claude_code",
        }
        allowed_funcs = build_condition_helpers(context=context)
        allowed_funcs["requires_task_for_any_touched_file"] = requires_task_for_any_touched_file

        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        result = evaluator.evaluate(condition)
        assert result is False, "Should not block when editing a plan file"

    def test_when_condition_blocks_non_plan_file(self) -> None:
        """Editing a non-plan file without task should still block."""
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

        condition = (
            "variables.get('require_task_before_edit') and not variables.get('task_claimed') "
            "and event.data.get('canonical_repo_mutation') "
            "and requires_task_for_any_touched_file(tool_input, source, variables.get('plan_mode'))"
        )

        context = {
            "variables": {
                "require_task_before_edit": True,
                "task_claimed": False,
                "plan_mode": False,
            },
            "event": type("Event", (), {"data": {"canonical_repo_mutation": True}})(),
            "tool_input": {"file_path": "/project/src/main.py"},
            "source": "claude_code",
        }
        allowed_funcs = build_condition_helpers(context=context)
        allowed_funcs["requires_task_for_any_touched_file"] = requires_task_for_any_touched_file

        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        result = evaluator.evaluate(condition)
        assert result is True, "Should block non-plan file without task"

    def test_plan_mode_exempts_markdown(self) -> None:
        """In plan mode, writing .md files should not be blocked."""
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

        condition = (
            "variables.get('require_task_before_edit') and not variables.get('task_claimed') "
            "and event.data.get('canonical_repo_mutation') "
            "and requires_task_for_any_touched_file(tool_input, source, variables.get('plan_mode'))"
        )

        context = {
            "variables": {
                "require_task_before_edit": True,
                "task_claimed": False,
                "plan_mode": True,
            },
            "event": type("Event", (), {"data": {"canonical_repo_mutation": True}})(),
            "tool_input": {"file_path": "/project/docs/plans/my-plan.md"},
            "source": "claude_code",
        }
        allowed_funcs = build_condition_helpers(context=context)
        allowed_funcs["requires_task_for_any_touched_file"] = requires_task_for_any_touched_file

        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        result = evaluator.evaluate(condition)
        assert result is False, "Should not block markdown in plan mode"

    def test_plan_mode_still_blocks_non_markdown(self) -> None:
        """In plan mode, writing non-.md files should still be blocked."""
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

        condition = (
            "variables.get('require_task_before_edit') and not variables.get('task_claimed') "
            "and requires_task_for_any_touched_file(tool_input, source, variables.get('plan_mode'))"
        )

        context = {
            "variables": {
                "require_task_before_edit": True,
                "task_claimed": False,
                "plan_mode": True,
            },
            "tool_input": {"file_path": "/project/src/main.py"},
            "source": "claude_code",
        }
        allowed_funcs = build_condition_helpers(context=context)
        allowed_funcs["requires_task_for_any_touched_file"] = requires_task_for_any_touched_file

        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        result = evaluator.evaluate(condition)
        assert result is True, "Should block non-markdown even in plan mode"

    def test_multi_file_blocks_when_any_touched_path_requires_task(self) -> None:
        """A mixed exempt/non-exempt patch should block as a whole."""
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

        condition = (
            "variables.get('require_task_before_edit') and not variables.get('task_claimed') "
            "and requires_task_for_any_touched_file(tool_input, source, variables.get('plan_mode'))"
        )

        context = {
            "variables": {
                "require_task_before_edit": True,
                "task_claimed": False,
                "plan_mode": False,
            },
            "tool_input": {
                "file_paths": ["/project/.gobby/plans/my-plan.md", "/project/src/main.py"],
            },
            "source": "claude_code",
        }
        allowed_funcs = build_condition_helpers(context=context)
        allowed_funcs["requires_task_for_any_touched_file"] = requires_task_for_any_touched_file

        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        result = evaluator.evaluate(condition)
        assert result is True, "Should block if any touched file needs a task"


class TestIsPlanFile:
    """Unit tests for is_plan_file helper."""

    def test_gobby_plans_md(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/project/.gobby/plans/my-plan.md") is True

    def test_claude_plans_md(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/home/user/.claude/plans/design.md") is True

    def test_non_md_file_in_plans_dir(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/project/.gobby/plans/notes.txt") is False

    def test_regular_source_file(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/project/src/main.py") is False

    def test_empty_path(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("") is False

    def test_md_file_outside_plans(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/project/docs/plan.md") is False

    def test_source_param_accepted(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/project/.gobby/plans/x.md", "claude_code") is True
        assert is_plan_file("/project/.gobby/plans/x.md", None) is True

    def test_gemini_deep_tmp_plan(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/home/user/.gemini/tmp/abc123/plans/design.md") is True

    def test_gemini_notes_md(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/home/user/.gemini/notes.md") is True

    def test_codex_plans_md(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/home/user/.codex/plans/plan.md") is True

    def test_gemini_config_json_rejected(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/home/user/.gemini/config.json") is False

    def test_codex_config_toml_rejected(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/home/user/.codex/config.toml") is False


class TestTouchedFileHelpers:
    """Unit tests for touched-path task gating helpers."""

    def test_get_touched_file_paths_prefers_file_paths(self) -> None:
        from gobby.workflows.enforcement.blocking import get_touched_file_paths

        result = get_touched_file_paths(
            {
                "file_paths": ["/project/a.py", "/project/b.py"],
                "file_path": "/project/ignored.py",
            }
        )

        assert result == ["/project/a.py", "/project/b.py"]

    def test_get_touched_file_paths_falls_back_to_changes(self) -> None:
        from gobby.workflows.enforcement.blocking import get_touched_file_paths

        result = get_touched_file_paths(
            {
                "changes": [
                    {"path": "/project/a.py"},
                    {"file_path": "/project/b.py"},
                    {"path": "/project/a.py"},
                ]
            }
        )

        assert result == ["/project/a.py", "/project/b.py"]

    def test_requires_task_for_any_touched_file_allows_all_exempt_paths(self) -> None:
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        result = requires_task_for_any_touched_file(
            {
                "file_paths": [
                    "/project/.gobby/plans/plan.md",
                    "/project/.codex/notes.md",
                ]
            },
            source="codex",
            plan_mode=False,
        )

        assert result is False

    def test_requires_task_for_any_touched_file_blocks_mixed_paths(self) -> None:
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        result = requires_task_for_any_touched_file(
            {
                "file_paths": [
                    "/project/.gobby/plans/plan.md",
                    "/project/src/main.py",
                ]
            },
            source="codex",
            plan_mode=False,
        )

        assert result is True

    def test_requires_task_for_any_touched_file_allows_markdown_in_plan_mode(self) -> None:
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        result = requires_task_for_any_touched_file(
            {"file_paths": ["/project/docs/notes.md"]},
            source="codex",
            plan_mode=True,
        )

        assert result is False

    def test_requires_task_for_any_touched_file_fails_closed_without_paths(self) -> None:
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        result = requires_task_for_any_touched_file(
            {"patch": "*** Begin Patch\n*** End Patch\n"},
            source="codex",
            plan_mode=False,
        )

        assert result is True


class TestRequireCleanTreeBeforeStatus:
    """Verify require-clean-tree-before-status blocks on dirty files."""

    def test_blocks_close_task_mcp(self, db, manager) -> None:
        """Should block gobby-tasks:close_task and de_escalate_task."""
        _sync_bundled(db)

        row = manager.get_by_name("require-clean-tree-before-status")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"
        assert "gobby-tasks:close_task" in body.effects[0].mcp_tools
        assert "gobby-tasks:de_escalate_task" in body.effects[0].mcp_tools

    def test_when_checks_dirty_files(self, db, manager) -> None:
        """Should check has_dirty_files but not task_has_commits."""
        _sync_bundled(db)

        row = manager.get_by_name("require-clean-tree-before-status")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "has_dirty_files" in body.when
        assert "task_has_commits" not in body.when

    def test_error_message_mentions_uncommitted(self, db, manager) -> None:
        """Error message should specifically mention uncommitted changes."""
        _sync_bundled(db)

        row = manager.get_by_name("require-clean-tree-before-status")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        reason = body.effects[0].reason or ""
        assert "uncommitted" in reason.lower()

    def test_higher_priority_than_commit_rule(self, db, manager) -> None:
        """Should fire before require-commit-before-status (lower number = higher priority)."""
        _sync_bundled(db)

        dirty_rule = manager.get_by_name("require-clean-tree-before-status")
        commit_rule = manager.get_by_name("require-commit-before-status")

        assert dirty_rule.priority < commit_rule.priority


class TestRequireCommitBeforeStatus:
    """Verify require-commit-before-status requires commit before status transitions."""

    def test_blocks_close_task_mcp(self, db, manager) -> None:
        """Should block gobby-tasks:close_task and de_escalate_task."""
        _sync_bundled(db)

        row = manager.get_by_name("require-commit-before-status")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"
        assert "gobby-tasks:close_task" in body.effects[0].mcp_tools
        assert "gobby-tasks:de_escalate_task" in body.effects[0].mcp_tools

    def test_when_checks_commits_and_reasons(self, db, manager) -> None:
        """Should check task_has_commits and special close reasons."""
        _sync_bundled(db)

        row = manager.get_by_name("require-commit-before-status")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "task_has_commits" in body.when
        assert "commit_sha" in body.when

    def test_when_checks_session_edited_files_and_dirty(self, db, manager) -> None:
        """Should only require commit when session has changes."""
        _sync_bundled(db)

        row = manager.get_by_name("require-commit-before-status")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert "session_edited_files" in body.when
        assert "has_dirty_files" in body.when

    def test_error_message_mentions_no_commit(self, db, manager) -> None:
        """Error message should specifically mention no commit linked."""
        _sync_bundled(db)

        row = manager.get_by_name("require-commit-before-status")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        reason = body.effects[0].reason or ""
        assert "no commit linked" in reason.lower()


class TestStripSkipValidationWithCommit:
    """Verify strip-skip-validation-with-commit rewrites skip_validation."""

    def test_rewrites_close_task(self, db, manager) -> None:
        """Should use rewrite_input + inject_context effects."""
        _sync_bundled(db)

        row = manager.get_by_name("strip-skip-validation-with-commit")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        effect_types = {e.type for e in body.resolved_effects}
        assert "rewrite_input" in effect_types
        assert "inject_context" in effect_types

    def test_when_checks_skip_validation(self, db, manager) -> None:
        """Should check skip_validation flag."""
        _sync_bundled(db)

        row = manager.get_by_name("strip-skip-validation-with-commit")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "skip_validation" in body.when


class TestBlockAskDuringStopCompliance:
    """Verify block-ask-during-stop-compliance blocks questions during stop."""

    def test_blocks_ask_user_question(self, db, manager) -> None:
        """Should block AskUserQuestion."""
        _sync_bundled(db)

        row = manager.get_by_name("block-ask-during-stop-compliance")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"
        assert "AskUserQuestion" in body.effects[0].tools

    def test_when_checks_stop_attempts_and_task(self, db, manager) -> None:
        """Should check stop_attempts and task_claimed."""
        _sync_bundled(db)

        row = manager.get_by_name("block-ask-during-stop-compliance")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "stop_attempts" in body.when
        assert "task_claimed" in body.when


class TestTrackTaskClaim:
    """Verify track-task-claim observes task claim events."""

    def test_sets_task_claimed_true(self, db, manager) -> None:
        """Should use observe effect to track task claims."""
        _sync_bundled(db)

        row = manager.get_by_name("track-task-claim")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "after_tool"
        assert body.effects[0].type == "observe"

    def test_when_matches_claim_and_create(self, db, manager) -> None:
        """Should fire on claim_task and create_task."""
        _sync_bundled(db)

        row = manager.get_by_name("track-task-claim")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "claim_task" in body.when
        assert "create_task" in body.when


class TestBlockReopenTask:
    """Verify reopen is reserved for explicit human-driven lifecycle resets."""

    def test_blocks_reopen_task_calls(self, db, manager) -> None:
        """Should block the gobby-tasks reopen_task MCP call."""
        _sync_bundled(db)

        row = manager.get_by_name("block-reopen-task")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"
        assert "reopen_task" in (body.when or "")

    def test_when_checks_claimed_tasks(self, db, manager) -> None:
        """Should only block when the target task is in this session's claimed_tasks."""
        _sync_bundled(db)

        row = manager.get_by_name("block-reopen-task")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "claimed_tasks" in body.when
        assert "task_id" in body.when

    def test_reason_mentions_de_escalation(self, db, manager) -> None:
        """Guidance should route escalated work through de_escalate_task."""
        _sync_bundled(db)

        row = manager.get_by_name("block-reopen-task")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        reason = body.effects[0].reason or ""
        assert "de_escalate_task" in reason

    @pytest.mark.asyncio
    async def test_blocks_reopen_for_claimed_task_only(self, db) -> None:
        """A reopen call should block only when the task is claimed by this session."""
        _sync_bundled(db)
        engine = RuleEngine(db)

        claimed_event = _make_reopen_event("#1")
        claimed_variables = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-1": "#1"},
            "loaded_skills": ["task-transitions"],
        }
        claimed_response = await engine.evaluate(
            claimed_event,
            session_id="sess-1",
            variables=claimed_variables,
        )

        assert claimed_response.decision == "block"
        assert "reopen_task is blocked" in (claimed_response.reason or "")

        unclaimed_event = _make_reopen_event("#2")
        unclaimed_response = await engine.evaluate(
            unclaimed_event,
            session_id="sess-1",
            variables=claimed_variables,
        )

        assert unclaimed_response.decision == "allow"

    @pytest.mark.asyncio
    async def test_allows_reopen_for_task_claimed_by_other_session(self, db) -> None:
        """A task absent from this session's claimed_tasks should not be blocked."""
        _sync_bundled(db)
        engine = RuleEngine(db)

        response = await engine.evaluate(
            _make_reopen_event("#77"),
            session_id="sess-1",
            variables={
                "task_claimed": True,
                "claimed_tasks": {"uuid-1": "#1"},
                "loaded_skills": ["task-transitions"],
            },
        )

        assert response.decision == "allow"


class TestBlockFrontHalfOnInteractiveLock:
    """Verify block-front-half-on-interactive-lock is the mutex between the
    interactive plan-adversary loop and the autonomous front-half orchestrator.
    """

    def test_rule_syncs(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("block-front-half-on-interactive-lock")
        assert row is not None

    def test_blocks_front_half_tick(self, db, manager) -> None:
        """Rule targets the gobby-tasks-ops front_half_tick MCP call with a block effect."""
        _sync_bundled(db)
        row = manager.get_by_name("block-front-half-on-interactive-lock")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"
        assert "front_half_tick" in (body.when or "")
        assert "gobby-tasks-ops" in (body.when or "")

    def test_uses_task_has_label_prefix_helper(self, db, manager) -> None:
        """The condition must call task_has_label_prefix on the target task with the
        interactive-planning-in-progress: prefix — that's what makes it detect any
        session's lock without caring about ownership."""
        _sync_bundled(db)
        row = manager.get_by_name("block-front-half-on-interactive-lock")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert "task_has_label_prefix" in (body.when or "")
        assert "interactive:planning-in-progress:" in (body.when or "")

    def test_reason_explains_recovery(self, db, manager) -> None:
        """The block reason must tell the operator how to clear the lock."""
        _sync_bundled(db)
        row = manager.get_by_name("block-front-half-on-interactive-lock")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        reason = body.effects[0].reason or ""
        assert "interactive plan-adversary" in reason
        assert "lock" in reason

    def test_when_condition_blocks_locked_parent_and_allows_unlocked(self) -> None:
        """End-to-end: the when-expression evaluates True when the parent carries
        ANY interactive:planning-in-progress:* label, and False otherwise."""
        from gobby.workflows.safe_evaluator import (
            SafeExpressionEvaluator,
            build_condition_helpers,
        )

        class _FakeTask:
            def __init__(self, labels: list[str] | None) -> None:
                self.labels = labels

        class _FakeTaskManager:
            def __init__(self, labels: list[str] | None) -> None:
                self._t = _FakeTask(labels)

            def get_task(self, _task_id: str) -> _FakeTask:
                return self._t

        condition = (
            "event.data.get('tool_name') in ('call_tool', 'mcp__gobby__call_tool') "
            "and tool_input.get('server_name') == 'gobby-tasks-ops' "
            "and tool_input.get('tool_name') == 'front_half_tick' "
            "and task_has_label_prefix("
            "  (tool_input.get('arguments') or {}).get('task_id'),"
            "  'interactive:planning-in-progress:')"
        )

        event = type("E", (), {"data": {"tool_name": "mcp__gobby__call_tool"}})()
        tool_input = {
            "server_name": "gobby-tasks-ops",
            "tool_name": "front_half_tick",
            "arguments": {"task_id": "#42"},
        }
        ctx = {"event": event, "tool_input": tool_input}

        # Locked parent → blocks
        locked_funcs = build_condition_helpers(
            task_manager=_FakeTaskManager(
                ["planning-round:1", "interactive:planning-in-progress:sess-a"]
            ),
            context=ctx,
        )
        assert SafeExpressionEvaluator(ctx, locked_funcs).evaluate(condition) is True

        # No lock → does not block
        unlocked_funcs = build_condition_helpers(
            task_manager=_FakeTaskManager(["planning-round:1"]),
            context=ctx,
        )
        assert SafeExpressionEvaluator(ctx, unlocked_funcs).evaluate(condition) is False

        # Different MCP call → does not block
        tool_input_other = {
            "server_name": "gobby-tasks-ops",
            "tool_name": "something_else",
            "arguments": {"task_id": "#42"},
        }
        ctx_other = {"event": event, "tool_input": tool_input_other}
        assert SafeExpressionEvaluator(ctx_other, locked_funcs).evaluate(condition) is False


class TestInjectTransitionSkill:
    """Verify lifecycle schemas prompt for the task-transitions skill."""

    def test_when_mentions_extended_lifecycle_tools(self, db, manager) -> None:
        """reopen/escalate/de_escalate should all trigger the skill directive."""
        _sync_bundled(db)

        row = manager.get_by_name("inject-transition-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "after_tool"
        assert "reopen_task" in (body.when or "")
        assert "escalate_task" in (body.when or "")
        assert "de_escalate_task" in (body.when or "")
        assert "mark_task_review_rejected" in (body.when or "")

    def test_emits_task_transitions_directive(self, db, manager) -> None:
        """The rule should emit a directive without writing skill ledgers."""
        _sync_bundled(db)

        row = manager.get_by_name("inject-transition-skill")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        inject_effects = [effect for effect in body.effects if effect.type == "inject_context"]
        set_effects = [effect for effect in body.effects if effect.type == "set_variable"]

        assert len(inject_effects) == 1
        assert (
            inject_effects[0].template
            == 'Call get_skill(name="task-transitions") on gobby-skills, then continue.'
        )
        assert set_effects == []


class TestTaskLifecycleSkillGates:
    """Verify lifecycle calls require agent-loaded task skills."""

    def test_creation_gate_blocks_without_loaded_skill(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-task-creation-skill-loaded")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert "skill_loaded('task-creation')" in (body.when or "")
        assert 'Call get_skill(name="task-creation") on gobby-skills, then continue.' in (
            body.effects[0].reason or ""
        )

    def test_transition_gate_blocks_without_loaded_skill(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-task-transitions-skill-loaded")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert "reopen_task" in (body.when or "")
        assert "skill_loaded('task-transitions')" in (body.when or "")
        assert 'Call get_skill(name="task-transitions") on gobby-skills, then continue.' in (
            body.effects[0].reason or ""
        )

    @pytest.mark.asyncio
    async def test_creation_gate_blocks_normalized_call_until_loaded(self, db, manager) -> None:
        _sync_bundled(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-session",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "mcp_server": "gobby-tasks",
                "mcp_tool": "create_task",
                "tool_name": "mcp__gobby-tasks__create_task",
                "tool_input": {"title": "Work"},
            },
        )

        blocked = await RuleEngine(db).evaluate(event, session_id="sid", variables={})
        allowed = await RuleEngine(db).evaluate(
            event,
            session_id="sid",
            variables={"loaded_skills": ["task-creation"]},
        )

        assert blocked.decision == "block"
        assert "task-creation" in (blocked.reason or "")
        assert allowed.decision == "allow"

    @pytest.mark.asyncio
    async def test_transition_gate_blocks_raw_call_until_loaded(self, db, manager) -> None:
        _sync_bundled(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-session",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "#42"},
                },
            },
        )

        blocked = await RuleEngine(db).evaluate(event, session_id="sid", variables={})
        allowed = await RuleEngine(db).evaluate(
            event,
            session_id="sid",
            variables={"loaded_skills": ["task-transitions"]},
        )

        assert blocked.decision == "block"
        assert "task-transitions" in (blocked.reason or "")
        assert allowed.decision == "allow"


def _make_reopen_event(task_id: str) -> HookEvent:
    """Create a direct MCP reopen_task before_tool event."""
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session-ext",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "reopen_task",
                "arguments": {"task_id": task_id},
            },
        },
        metadata={"_platform_session_id": "test-session"},
    )
