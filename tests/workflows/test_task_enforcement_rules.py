"""Tests for task-enforcement.yaml rules.

Verifies blocking rules for native task tools, edit gating, commit
requirements, validation bypass prevention, stop compliance, and
task claim/release tracking.
"""

from __future__ import annotations

import shlex
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.skills.formatting import (
    skill_fetch_directive,
    skill_fetch_proxy_path,
)
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.git_utils import DirtyFiles
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

# Session id columns are native uuid in PostgreSQL; synthetic ids like
# SESSION_ID would fail with `invalid input syntax for type uuid`.
SESSION_ID = "11111111-1111-4111-8111-111111111111"
EXTERNAL_SESSION_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _sync_bundled(db: HubDatabase) -> object:
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    # Mark templates as installed so get_by_name() finds them
    db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return result


def _skill_fetch_template(name: str) -> str:
    return f'{{{{ skill_fetch_directive("{name}") }}}}'


def _close_task_event(
    task_id: str = "#1",
    *,
    commit_sha: str | None = "abc123",
    preview: bool = False,
) -> HookEvent:
    arguments: dict[str, object] = {"task_id": task_id}
    if commit_sha is not None:
        arguments["commit_sha"] = commit_sha
    if preview:
        arguments["preview"] = True
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        cwd="/tmp",
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": arguments,
            },
        },
        metadata={"_platform_session_id": SESSION_ID, "project_path": "/tmp"},
    )


def _status_gate_variables(
    *,
    claimed_tasks: dict[str, str] | None = None,
    active_task_id: str | None = None,
    task_edited_files: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    claimed = claimed_tasks or {"task-1": "#1"}
    return {
        "require_commit_before_status": True,
        "loaded_skills": ["tasks"],
        "task_claimed": bool(claimed),
        "claimed_tasks": claimed,
        "active_task_id": active_task_id,
        "task_edited_files": task_edited_files or {},
        "task_has_commits": True,
    }


async def _evaluate_close_event(
    db: HubDatabase,
    variables: dict[str, object],
    *,
    commit_sha: str | None = "abc123",
    preview: bool = False,
) -> HookResponse:
    _sync_bundled(db)
    SessionVariableManager(db).merge_variables(SESSION_ID, variables)
    handler = WorkflowHookHandler(rule_engine=RuleEngine(db))
    return await handler._evaluate_rules(
        _close_task_event("#1", commit_sha=commit_sha, preview=preview)
    )


TASK_ENFORCEMENT_RULES = {
    "block-cross-session-foreign-dirty-edit",
    "block-native-task-tools-unclaimed",
    "block-native-todo-write",
    "block-reopen-task",
    "require-task-creation-skill-on-schema",
    "require-task-transitions-skill-on-lifecycle",
    "require-task-creation-skill-loaded",
    "require-task-transitions-skill-loaded",
    "require-task-before-edit",
    "require-task-before-commit",
    "require-claimed-task-required-skills",
    "require-commit-before-status",
    "require-clean-tree-before-status",
    "task-commit-project-path-allowlist-before-git",
    "block-ask-during-stop-compliance",
    "block-needs-review-interactive",
    "track-task-claim",
    "reset-subagent-flag",
}

REPLACED_TASK_SKILL_RULES = {
    "inject-task-creation-on-schema",
    "inject-transition-skill",
}


class TestTaskEnforcementSync:
    """Test that task-enforcement.yaml syncs correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All task-enforcement rules should sync to rule_definitions."""
        _sync_bundled(db)

        rules = manager.list_all()
        rule_names = {r.name for r in rules}

        assert TASK_ENFORCEMENT_RULES.issubset(rule_names), (
            f"Missing: {TASK_ENFORCEMENT_RULES - rule_names}"
        )
        assert REPLACED_TASK_SKILL_RULES.isdisjoint(rule_names)
        assert "block-front-half-on-interactive-lock" not in rule_names

    def test_all_rules_have_group(self, db, manager) -> None:
        """All rules should have group='task-enforcement'."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in TASK_ENFORCEMENT_RULES:
                body = row.definition_json
                assert body.get("group") == "task-enforcement", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in TASK_ENFORCEMENT_RULES:
                body = RuleDefinitionBody.model_validate(row.definition_json)
                effect_types = {e.type for e in body.resolved_effects}
                assert effect_types <= {
                    "block",
                    "set_variable",
                    "observe",
                    "inject_context",
                    "mcp_call",
                    "rewrite_input",
                }


class TestRequireTaskBeforeCommit:
    """Explicit repository commits require an active task mandate."""

    @staticmethod
    async def _evaluate(
        db: HubDatabase,
        command: str,
        *,
        task_claimed: bool = False,
        source: SessionSource = SessionSource.CODEX,
    ) -> HookResponse:
        _sync_bundled(db)
        data: dict[str, object] = {
            "tool_name": "exec_command",
            "tool_input": {"command": command},
        }
        normalize_tool_fields(data)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=source,
            timestamp=datetime.now(UTC),
            data=data,
            metadata={},
        )
        return await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "require_task_before_edit": True,
                "task_claimed": task_claimed,
            },
        )

    @pytest.mark.asyncio
    async def test_blocks_taskless_commit_with_tracked_plan_artifact(self, db: HubDatabase) -> None:
        response = await self._evaluate(db, "git commit -m 'record plan'")

        assert response.decision == "block"
        assert response.reason is not None
        assert "claim the task that authorizes this repository commit" in response.reason

    @pytest.mark.asyncio
    async def test_claimed_task_allows_commit(self, db: HubDatabase) -> None:
        response = await self._evaluate(
            db,
            "git commit -m 'record plan'",
            task_claimed=True,
        )

        assert response.decision == "allow"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "git merge --no-edit topic-branch",
            "git diff --cached -- .gobby/plans/implementation.md",
        ],
    )
    async def test_merge_and_read_only_plan_paths_are_preserved(
        self,
        db: HubDatabase,
        command: str,
    ) -> None:
        response = await self._evaluate(db, command)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_pipeline_backup_commit_is_preserved(self, db: HubDatabase) -> None:
        response = await self._evaluate(
            db,
            "git commit -m 'chore: backup task state'",
            source=SessionSource.PIPELINE,
        )

        assert response.decision == "allow"


class TestBlockNativeTaskToolsUnclaimed:
    """Verify block-native-task-tools-unclaimed blocks task tools without a Gobby task."""

    def test_blocks_task_tools_when_unclaimed(self, db, manager) -> None:
        """Should block TaskCreate, TaskUpdate, TaskGet, TaskList."""
        _sync_bundled(db)

        row = manager.get_by_name("block-native-task-tools-unclaimed")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"

        expected_tools = {"TaskCreate", "TaskUpdate", "TaskGet", "TaskList"}
        assert set(body.effects[0].tools) == expected_tools

    def test_when_checks_is_subagent_and_task_claimed(self, db, manager) -> None:
        """Should only block when not subagent AND no task claimed."""
        _sync_bundled(db)

        row = manager.get_by_name("block-native-task-tools-unclaimed")
        body = RuleDefinitionBody.model_validate(row.definition_json)

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

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"
        assert set(body.effects[0].tools) == {"TodoWrite"}

    def test_when_only_checks_is_subagent(self, db, manager) -> None:
        """Should block for all non-subagent sessions regardless of task_claimed."""
        _sync_bundled(db)

        row = manager.get_by_name("block-native-todo-write")
        body = RuleDefinitionBody.model_validate(row.definition_json)

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

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"
        assert body.effects[0].tools in (None, [])

    def test_when_checks_task_claimed_mutation_and_plan_mode(self, db, manager) -> None:
        """Should check task_claimed and multi-file task gating."""
        _sync_bundled(db)

        row = manager.get_by_name("require-task-before-edit")
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.when is not None
        assert "task_claimed" in body.when
        assert "canonical_repo_mutation" in body.when
        assert "requires_task_for_any_touched_file" in body.when
        assert "plan_mode" in body.when

    @pytest.mark.asyncio
    async def test_workbook_diagnostic_heredoc_is_not_treated_as_an_edit(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        read_only = """python3 - <<'PYEOF'
from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET
base = Path('/tmp/workbook-extract')
for path in sorted(base.glob('*.xlsx')):
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read('xl/workbook.xml'))
        print(path.relative_to(base), root.find('.//sheet').text)
PYEOF"""
        mutating = """python3 - <<'PYEOF'
from pathlib import Path
Path('result.txt').write_text('changed')
PYEOF"""
        variables = {
            "require_task_before_edit": True,
            "task_claimed": False,
            "loaded_skills": ["python", "tasks"],
            "brevity_disabled": True,
            "skill_discovery_instructions_shown": True,
        }

        for command, expected_kind, expected_decision in (
            (read_only, "execute", "allow"),
            (mutating, "write", "block"),
        ):
            data: dict[str, object] = {
                "tool_name": "Bash",
                "tool_input": {"command": command},
            }
            normalize_tool_fields(data)
            event = HookEvent(
                event_type=HookEventType.BEFORE_TOOL,
                session_id=SESSION_ID,
                source=SessionSource.CODEX,
                timestamp=datetime.now(UTC),
                data=data,
            )

            response = await RuleEngine(db).evaluate(
                event,
                session_id=SESSION_ID,
                variables=variables,
            )

            assert data["canonical_tool_kind"] == expected_kind
            assert response.decision == expected_decision

    @pytest.mark.asyncio
    async def test_indeterminate_aws_mcp_diagnostic_does_not_require_task(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        script = """
import asyncio
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPServerConfig


async def main():
    config = MCPServerConfig(
        name="aws-openapi-smoke",
        project_id="00000000-0000-0000-0000-000000000001",
        transport="stdio",
        command="uvx",
        args=["awslabs.openapi-mcp-server@1.1.5"],
    )
    manager = MCPClientManager([config])
    try:
        await manager.list_tools("aws-openapi-smoke")
        session = await manager.get_client_session("aws-openapi-smoke")
        await session.list_prompts()
        await session.list_resources()
    finally:
        await manager.disconnect_all()


asyncio.run(main())
"""
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": f"uv run python -c {shlex.quote(script)}"},
        }
        normalize_tool_fields(data)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data=data,
        )

        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "require_task_before_edit": True,
                "task_claimed": False,
                "plan_mode": False,
            },
        )

        assert data["canonical_tool_kind"] == "execute"
        assert data["canonical_tool_confidence"] == "low"
        assert "canonical_repo_mutation" not in data
        assert response.decision == "allow"

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

    def test_when_condition_blocks_shell_write_workaround(self) -> None:
        """Shell writes should carry canonical mutation/path metadata into task gating."""
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

        condition = (
            "variables.get('require_task_before_edit') and not variables.get('task_claimed') "
            "and event.data.get('canonical_repo_mutation') "
            "and requires_task_for_any_touched_file(tool_input, source, variables.get('plan_mode'))"
        )
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "printf hello > src/main.py"},
        }
        normalize_tool_fields(data)

        context = {
            "variables": {
                "require_task_before_edit": True,
                "task_claimed": False,
                "plan_mode": False,
            },
            "event": type("Event", (), {"data": data})(),
            "tool_input": data["tool_input"],
            "source": "claude_code",
        }
        allowed_funcs = build_condition_helpers(context=context)
        allowed_funcs["requires_task_for_any_touched_file"] = requires_task_for_any_touched_file

        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        result = evaluator.evaluate(condition)
        assert result is True, "Should block shell write to source file without task"

    def test_when_condition_exempts_shell_write_to_plan_file(self) -> None:
        """A shell write whose canonical paths are all plan files stays exempt."""
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

        condition = (
            "variables.get('require_task_before_edit') and not variables.get('task_claimed') "
            "and event.data.get('canonical_repo_mutation') "
            "and requires_task_for_any_touched_file("
            "tool_input, source, variables.get('plan_mode'), event.data)"
        )
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "printf hello > .gobby/plans/my-plan.md"},
        }
        normalize_tool_fields(data)
        assert data.get("canonical_repo_mutation") is True

        context = {
            "variables": {
                "require_task_before_edit": True,
                "task_claimed": False,
                "plan_mode": False,
            },
            "event": type("Event", (), {"data": data})(),
            "tool_input": data["tool_input"],
            "source": "claude_code",
        }
        allowed_funcs = build_condition_helpers(context=context)
        allowed_funcs["requires_task_for_any_touched_file"] = requires_task_for_any_touched_file

        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        result = evaluator.evaluate(condition)
        assert result is False, "Shell writes touching only plan files must not require a task"

    def test_when_condition_blocks_shell_write_mixing_plan_and_source(self) -> None:
        """A shell write touching a plan file AND source still requires a task."""
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

        condition = (
            "variables.get('require_task_before_edit') and not variables.get('task_claimed') "
            "and event.data.get('canonical_repo_mutation') "
            "and requires_task_for_any_touched_file("
            "tool_input, source, variables.get('plan_mode'), event.data)"
        )
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "printf hi > .gobby/plans/my-plan.md && printf hi > src/main.py"
            },
        }
        normalize_tool_fields(data)

        context = {
            "variables": {
                "require_task_before_edit": True,
                "task_claimed": False,
                "plan_mode": False,
            },
            "event": type("Event", (), {"data": data})(),
            "tool_input": data["tool_input"],
            "source": "claude_code",
        }
        allowed_funcs = build_condition_helpers(context=context)
        allowed_funcs["requires_task_for_any_touched_file"] = requires_task_for_any_touched_file

        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        result = evaluator.evaluate(condition)
        assert result is True, "Mixed plan/source shell writes must still require a task"

    def test_requires_task_fails_closed_without_any_paths(self) -> None:
        """No structured and no canonical paths keeps the fail-closed contract."""
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        assert (
            requires_task_for_any_touched_file(
                {"command": "uv run python - <<'EOF'\nprint('opaque')\nEOF"},
                "claude_code",
                False,
                {"canonical_repo_mutation": True},
            )
            is True
        )

    def test_requires_task_canonical_path_string_fallback(self) -> None:
        """A bare canonical_file_path string is honored when the list is absent."""
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        event_data = {
            "canonical_repo_mutation": True,
            "canonical_file_path": ".claude/plans/current-plan.md",
        }
        assert (
            requires_task_for_any_touched_file({"command": "x"}, "claude_code", False, event_data)
            is False
        )

    def test_when_condition_ignores_stderr_suppression(self) -> None:
        """Read-only commands with benign redirects carry no mutation metadata."""
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

        condition = (
            "variables.get('require_task_before_edit') and not variables.get('task_claimed') "
            "and event.data.get('canonical_repo_mutation') "
            "and requires_task_for_any_touched_file(tool_input, source, variables.get('plan_mode'))"
        )
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -r pattern src 2>/dev/null"},
        }
        normalize_tool_fields(data)

        context = {
            "variables": {
                "require_task_before_edit": True,
                "task_claimed": False,
                "plan_mode": False,
            },
            "event": type("Event", (), {"data": data})(),
            "tool_input": data["tool_input"],
            "source": "claude_code",
        }
        allowed_funcs = build_condition_helpers(context=context)
        allowed_funcs["requires_task_for_any_touched_file"] = requires_task_for_any_touched_file

        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        result = evaluator.evaluate(condition)
        assert not result, "Stderr suppression on a read-only command must not require a task"

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


class TestRequireClaimedTaskRequiredSkills:
    """Verify claimed task metadata gates first source-code write."""

    CONDITION = (
        "event.data.get('canonical_tool_kind') == 'write' "
        "and claimed_task_source_code_write(tool_input, event.data) "
        "and missing_claimed_task_required_skills(tool_input, event.data) != []"
    )

    def _eval(
        self,
        *,
        file_path: str = "/project/src/main.py",
        required_skills: list[str] | None = None,
        language_skills: list[str] | None = None,
        loaded_skills: list[str] | None = None,
        canonical_tool_kind: str = "write",
    ) -> bool:
        from types import SimpleNamespace

        from gobby.workflows.enforcement.blocking import claimed_task_source_code_write
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

        context = {
            "variables": {
                "claimed_task_required_skills": required_skills or [],
                "claimed_task_language_skills": language_skills or [],
                "loaded_skills": loaded_skills or [],
            },
            "event": SimpleNamespace(
                data={
                    "canonical_tool_kind": canonical_tool_kind,
                    "canonical_file_path": file_path,
                }
            ),
            "tool_input": {"file_path": file_path},
        }
        allowed_funcs = build_condition_helpers(context=context)
        allowed_funcs["claimed_task_source_code_write"] = claimed_task_source_code_write

        evaluator = SafeExpressionEvaluator(context=context, allowed_funcs=allowed_funcs)
        return evaluator.evaluate(self.CONDITION)

    def test_rule_syncs_with_dynamic_skill_directive(self, db, manager) -> None:
        _sync_bundled(db)

        row = manager.get_by_name("require-claimed-task-required-skills")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"
        helper = "missing_claimed_task_required_skills(tool_input, event.data)"
        assert helper in body.when
        assert "skill_fetch_batch_directive" in body.effects[0].reason

    def test_condition_blocks_inferred_python_from_task_metadata(self) -> None:
        assert (
            self._eval(
                required_skills=["python", "typescript", "development-discipline"],
                language_skills=["python", "typescript"],
            )
            is True
        )

    def test_condition_allows_python_edit_without_typescript_skill(self) -> None:
        assert (
            self._eval(
                required_skills=["python", "typescript", "development-discipline"],
                language_skills=["python", "typescript"],
                loaded_skills=["python", "development-discipline"],
            )
            is False
        )

    def test_condition_requires_typescript_for_tsx_edit(self) -> None:
        assert (
            self._eval(
                file_path="/project/web/app.tsx",
                required_skills=["python", "typescript", "development-discipline"],
                language_skills=["python", "typescript"],
                loaded_skills=["development-discipline"],
            )
            is True
        )

    def test_condition_blocks_tdd_required_metadata(self) -> None:
        assert (
            self._eval(
                file_path="/project/src/app.ts",
                required_skills=["test-driven-development"],
            )
            is True
        )

    def test_condition_skips_non_source_code_writes(self) -> None:
        assert (
            self._eval(
                file_path="/project/docs/notes.md",
                required_skills=["development-discipline"],
            )
            is False
        )

    def test_condition_skips_when_skills_loaded(self) -> None:
        assert (
            self._eval(
                required_skills=["python", "development-discipline"],
                loaded_skills=["python", "development-discipline"],
            )
            is False
        )

    @pytest.mark.parametrize(
        "source",
        [
            SessionSource.CLAUDE,
            SessionSource.CODEX,
            SessionSource.DROID,
            SessionSource.GROK,
            SessionSource.QWEN,
        ],
    )
    @pytest.mark.asyncio
    async def test_rule_blocks_with_every_missing_skill_in_load_order(
        self,
        db: HubDatabase,
        source: SessionSource,
    ) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=source,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "Write",
                "canonical_tool_kind": "write",
                "canonical_file_path": "/project/src/main.py",
                "tool_input": {"file_path": "/project/src/main.py"},
            },
            metadata={},
        )

        response = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "task_claimed": True,
                "claimed_task_required_skills": [
                    "tasks",
                    "python",
                    "development-discipline",
                ],
                "loaded_skills": [],
            },
        )

        assert response.decision == "block"
        assert response.reason is not None
        calls = [
            skill_fetch_proxy_path(skill) for skill in ("tasks", "python", "development-discipline")
        ]
        assert all(call in response.reason for call in calls)
        assert [response.reason.index(call) for call in calls] == sorted(
            response.reason.index(call) for call in calls
        )

    @pytest.mark.asyncio
    async def test_rule_allows_when_all_relevant_skills_loaded(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "Write",
                "canonical_tool_kind": "write",
                "canonical_file_path": "/project/src/main.py",
                "tool_input": {"file_path": "/project/src/main.py"},
            },
            metadata={},
        )

        response = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={
                "task_claimed": True,
                "enforce_tdd": False,
                "claimed_task_required_skills": [
                    "python",
                    "typescript",
                    "development-discipline",
                ],
                "claimed_task_language_skills": ["python", "typescript"],
                "loaded_skills": [
                    "python",
                    "development-discipline",
                    "context7",
                    "restraint",
                ],
            },
        )

        assert response.decision == "allow", response.reason

    @pytest.mark.asyncio
    async def test_existing_path_gate_still_blocks_without_task_metadata(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "Write",
                "canonical_tool_kind": "write",
                "canonical_file_path": "/project/src/lib.rs",
                "tool_input": {"file_path": "/project/src/lib.rs"},
            },
            metadata={},
        )

        response = await engine.evaluate(
            event,
            session_id=SESSION_ID,
            variables={"claimed_task_required_skills": [], "loaded_skills": []},
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert skill_fetch_directive("rust") in response.reason


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

    def test_codex_plans_md(self) -> None:
        from gobby.workflows.enforcement.blocking import is_plan_file

        assert is_plan_file("/home/user/.codex/plans/plan.md") is True

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

    @pytest.mark.parametrize(
        "feedback_path",
        [
            "docs/research/gobby-feedback/inbox/2026-08-26-codex-session-11117.md",
            "/project/docs/research/gobby-feedback/inbox/2026-08-26-claude-session-11103.md",
        ],
    )
    def test_requires_task_for_any_touched_file_allows_feedback_markdown(
        self, feedback_path: str
    ) -> None:
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        result = requires_task_for_any_touched_file(
            {"file_paths": [feedback_path]},
            source="codex",
            plan_mode=False,
        )

        assert result is False

    @pytest.mark.parametrize(
        "feedback_path",
        [
            "docs/research/gobby-feedback/inbox/report.json",
            "docs/research/gobby-feedback/report.md",
            "docs/research/gobby-feedback/inbox-adjacent/report.md",
            "docs/research/gobby-feedback",
            "docs/research/gobby-feedback/inbox-adjacent",
        ],
    )
    def test_requires_task_for_any_touched_file_rejects_feedback_lookalikes(
        self, feedback_path: str
    ) -> None:
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        result = requires_task_for_any_touched_file(
            {"file_paths": [feedback_path]},
            source="codex",
            plan_mode=False,
        )

        assert result is True

    def test_requires_task_for_any_touched_file_blocks_mixed_feedback_paths(self) -> None:
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        result = requires_task_for_any_touched_file(
            {
                "file_paths": [
                    "docs/research/gobby-feedback/inbox/session.md",
                    "src/gobby/workflows/enforcement/blocking.py",
                ]
            },
            source="codex",
            plan_mode=False,
        )

        assert result is True

    def test_requires_task_for_feedback_apply_patch_canonical_path(self) -> None:
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        feedback_path = (
            "docs/research/gobby-feedback/inbox/2026-08-26T073000-codex-session-11117.md"
        )
        result = requires_task_for_any_touched_file(
            {
                "patch": (
                    f"*** Begin Patch\n*** Add File: {feedback_path}\n+# Feedback\n*** End Patch"
                )
            },
            source="codex",
            plan_mode=False,
            event_data={
                "canonical_repo_mutation": True,
                "canonical_file_paths": [feedback_path],
            },
        )

        assert result is False

    @pytest.mark.parametrize(
        "inbox_dir",
        [
            "docs/research/gobby-feedback/inbox",
            "docs/research/gobby-feedback/inbox/",
            "/project/docs/research/gobby-feedback/inbox",
        ],
    )
    def test_requires_task_for_any_touched_file_allows_feedback_inbox_directory(
        self, inbox_dir: str
    ) -> None:
        """`mkdir -p` of the Git-ignored inbox is part of a taskless report write."""
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        result = requires_task_for_any_touched_file(
            {"file_paths": [inbox_dir]},
            source="claude",
            plan_mode=False,
        )

        assert result is False

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(
                "mkdir -p docs/research/gobby-feedback/inbox && cat > "
                "docs/research/gobby-feedback/inbox/2026-08-26T190000-claude-session-11103.md "
                "<<'EOF'\n---\nsession_id: 11103\n---\nEOF\n",
                id="mkdir-then-heredoc",
            ),
            pytest.param(
                "mkdir -p docs/research/gobby-feedback/inbox && cat > "
                "docs/research/gobby-feedback/inbox/2026-08-26T$(date +%H%M%S)"
                "-claude-session-11103.md <<'EOF'\n---\nsession_id: 11103\n---\nEOF\n",
                id="mkdir-then-heredoc-with-substitution",
            ),
            pytest.param(
                "cat > docs/research/gobby-feedback/inbox/"
                "2026-08-26T190000-claude-session-11103.md "
                "<<'EOF'\n---\nsession_id: 11103\n---\nEOF\n",
                id="heredoc-only",
            ),
        ],
    )
    def test_requires_task_for_feedback_inbox_shell_write_shapes(self, command: str) -> None:
        """The rule's documented inbox write passes through the shell adapter path (#21052)."""
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        data: dict[str, object] = {"tool_name": "Bash", "tool_input": {"command": command}}
        normalize_tool_fields(data)

        assert data["canonical_repo_mutation"] is True
        result = requires_task_for_any_touched_file(
            data["tool_input"], source="claude", plan_mode=False, event_data=data
        )

        assert result is False

    def test_substituted_feedback_inbox_heredoc_without_mkdir_is_pathless(self) -> None:
        """A substituted redirect target is the documented path-less execute residual.

        No repo mutation is recorded, so require-task-before-edit never reaches the
        helper; the helper alone keeps failing closed on a path-less write.
        """
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    "cat > docs/research/gobby-feedback/inbox/2026-08-26T$(date +%H%M%S)"
                    "-claude-session-11103.md <<'EOF'\n---\nsession_id: 11103\n---\nEOF\n"
                )
            },
        }
        normalize_tool_fields(data)

        assert data["canonical_tool_kind"] == "execute"
        assert "canonical_repo_mutation" not in data
        assert "canonical_file_paths" not in data
        result = requires_task_for_any_touched_file(
            data["tool_input"], source="claude", plan_mode=False, event_data=data
        )

        assert result is True

    def test_requires_task_for_feedback_inbox_mkdir_beside_a_source_write(self) -> None:
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file

        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    "mkdir -p docs/research/gobby-feedback/inbox && echo x > src/gobby/new.py"
                )
            },
        }
        normalize_tool_fields(data)

        result = requires_task_for_any_touched_file(
            data["tool_input"], source="claude", plan_mode=False, event_data=data
        )

        assert result is True

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

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"
        assert "gobby-tasks:close_task" in body.effects[0].mcp_tools
        assert "gobby-tasks:de_escalate_task" in body.effects[0].mcp_tools

    def test_when_checks_target_task_dirty_files(self, db, manager) -> None:
        """Should check only dirty files attributed to the target task."""
        _sync_bundled(db)

        row = manager.get_by_name("require-clean-tree-before-status")
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.when is not None
        assert "has_target_task_dirty_files" in body.when
        assert "preview" not in body.when
        assert "has_dirty_files" not in body.when
        assert "task_has_commits" not in body.when

    @pytest.mark.asyncio
    async def test_target_task_dirty_file_blocks(self, db) -> None:
        """Should block when the target task's attributed file is dirty."""
        variables = _status_gate_variables(
            active_task_id="task-1",
            task_edited_files={"task-1": ["src/owned.py"]},
        )

        with patch(
            "gobby.workflows.git_utils.get_dirty_files_categorized",
            return_value=DirtyFiles({"src/owned.py"}, set()),
        ):
            response = await _evaluate_close_event(db, variables, preview=True)

        assert response.decision == "block"
        assert response.reason is not None
        assert "uncommitted" in response.reason.lower()

    @pytest.mark.asyncio
    async def test_unrelated_dirty_file_allows(self, db) -> None:
        """Should allow dirty files not attributed to the target task."""
        variables = _status_gate_variables(
            active_task_id="task-1",
            task_edited_files={"task-1": ["src/owned.py"]},
        )

        with patch(
            "gobby.workflows.git_utils.get_dirty_files_categorized",
            return_value=DirtyFiles({"src/unrelated.py"}, set()),
        ):
            response = await _evaluate_close_event(db, variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_different_task_dirty_file_allows(self, db) -> None:
        """Should allow dirty files attributed only to another task."""
        variables = _status_gate_variables(
            claimed_tasks={"task-1": "#1", "task-2": "#2"},
            active_task_id="task-1",
            task_edited_files={"task-2": ["src/other.py"]},
        )

        with patch(
            "gobby.workflows.git_utils.get_dirty_files_categorized",
            return_value=DirtyFiles({"src/other.py"}, set()),
        ):
            response = await _evaluate_close_event(db, variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_unresolved_target_task_allows(self, db) -> None:
        """Should not run the clean-tree gate when the target task cannot resolve."""
        _sync_bundled(db)
        SessionVariableManager(db).merge_variables(
            SESSION_ID,
            _status_gate_variables(
                active_task_id="task-1",
                task_edited_files={"task-1": ["src/owned.py"]},
            ),
        )
        handler = WorkflowHookHandler(rule_engine=RuleEngine(db))

        with patch(
            "gobby.workflows.git_utils.get_dirty_files_categorized",
            return_value=DirtyFiles({"src/owned.py"}, set()),
        ):
            response = await handler._evaluate_rules(_close_task_event("#999"))

        assert response.decision == "allow"

    def test_error_message_mentions_uncommitted(self, db, manager) -> None:
        """Error message should specifically mention uncommitted changes."""
        _sync_bundled(db)

        row = manager.get_by_name("require-clean-tree-before-status")
        body = RuleDefinitionBody.model_validate(row.definition_json)

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

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"
        assert "gobby-tasks:close_task" in body.effects[0].mcp_tools
        assert "gobby-tasks:de_escalate_task" in body.effects[0].mcp_tools

    def test_when_checks_commits_and_reasons(self, db, manager) -> None:
        """Should check task_has_commits and special close reasons."""
        _sync_bundled(db)

        row = manager.get_by_name("require-commit-before-status")
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.when is not None
        assert "task_has_commits" in body.when
        assert "commit_sha" in body.when
        assert "preview" not in body.when

    @pytest.mark.asyncio
    async def test_conditional_close_preview_requires_commit_for_edits(self, db) -> None:
        variables = _status_gate_variables(
            active_task_id="task-1",
            task_edited_files={"task-1": ["src/owned.py"]},
        )
        variables["task_has_commits"] = False

        response = await _evaluate_close_event(
            db,
            variables,
            commit_sha=None,
            preview=True,
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert "no commit linked" in response.reason.lower()

    def test_when_checks_target_task_edits(self, db, manager) -> None:
        """Should only require commit when the target task has edits."""
        _sync_bundled(db)

        row = manager.get_by_name("require-commit-before-status")
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert "target_task_has_edits" in body.when
        assert "session_edited_files" not in body.when
        assert "has_dirty_files" not in body.when

    def test_error_message_mentions_no_commit(self, db, manager) -> None:
        """Error message should specifically mention no commit linked."""
        _sync_bundled(db)

        row = manager.get_by_name("require-commit-before-status")
        body = RuleDefinitionBody.model_validate(row.definition_json)

        reason = body.effects[0].reason or ""
        assert "no commit linked" in reason.lower()


class TestBlockNeedsReviewInteractive:
    """Verify interactive review tools stay blocked for interactive sessions."""

    @staticmethod
    def _review_event(
        tool_name: str,
        *,
        task_id: str = "anchor-1",
        stage_name: str = "planning",
    ) -> HookEvent:
        arguments = {"task_id": task_id, "stage_name": stage_name}
        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__call_tool",
                "mcp_server": "gobby-tasks-ops",
                "mcp_tool": tool_name,
                "tool_input": {
                    "server_name": "gobby-tasks-ops",
                    "tool_name": tool_name,
                    "arguments": arguments,
                },
            },
        )

    @pytest.mark.asyncio
    async def test_submit_for_review_blocks_interactive_session(self, db) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = {"loaded_skills": ["tasks"]}

        response = await engine.evaluate(
            self._review_event("submit_for_review"),
            session_id=SESSION_ID,
            variables=variables,
        )

        assert response.decision == "block"
        assert "block-needs-review-interactive" in (response.reason or "")

    @pytest.mark.asyncio
    async def test_submit_for_review_allows_spawned_agent(self, db) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = {
            "is_spawned_agent": True,
            "loaded_skills": ["tasks"],
        }

        response = await engine.evaluate(
            self._review_event("submit_for_review"),
            session_id=SESSION_ID,
            variables=variables,
        )

        assert response.decision == "allow"
        assert "block-needs-review-interactive" not in (response.reason or "")

    @pytest.mark.asyncio
    async def test_approve_review_blocks_interactive_session(self, db) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = {"loaded_skills": ["tasks"]}

        response = await engine.evaluate(
            self._review_event("approve_review"),
            session_id=SESSION_ID,
            variables=variables,
        )

        assert response.decision == "block"
        assert "block-needs-review-interactive" in (response.reason or "")


class TestBlockAskDuringStopCompliance:
    """Verify block-ask-during-stop-compliance blocks questions during stop."""

    def test_blocks_ask_user_question(self, db, manager) -> None:
        """Should block AskUserQuestion."""
        _sync_bundled(db)

        row = manager.get_by_name("block-ask-during-stop-compliance")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"
        assert "AskUserQuestion" in body.effects[0].tools

    def test_when_checks_stop_attempts_and_task(self, db, manager) -> None:
        """Should check stop_attempts and task_claimed."""
        _sync_bundled(db)

        row = manager.get_by_name("block-ask-during-stop-compliance")
        body = RuleDefinitionBody.model_validate(row.definition_json)

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

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "after_tool"
        assert body.effects[0].type == "observe"

    def test_when_matches_claim_and_create(self, db, manager) -> None:
        """Should fire on claim_task and create_task."""
        _sync_bundled(db)

        row = manager.get_by_name("track-task-claim")
        body = RuleDefinitionBody.model_validate(row.definition_json)

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

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"
        assert "reopen_task" in (body.when or "")

    def test_when_checks_claimed_tasks(self, db, manager) -> None:
        """Should only block when the target task is in this session's claimed_tasks."""
        _sync_bundled(db)

        row = manager.get_by_name("block-reopen-task")
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.when is not None
        assert "claimed_tasks" in body.when
        assert "task_id" in body.when

    def test_reason_mentions_de_escalation(self, db, manager) -> None:
        """Guidance should route escalated work through de_escalate_task."""
        _sync_bundled(db)

        row = manager.get_by_name("block-reopen-task")
        body = RuleDefinitionBody.model_validate(row.definition_json)

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
            "loaded_skills": ["tasks"],
        }
        claimed_response = await engine.evaluate(
            claimed_event,
            session_id=SESSION_ID,
            variables=claimed_variables,
        )

        assert claimed_response.decision == "block"
        assert "Use escalate_task" in (claimed_response.reason or "")

        unclaimed_event = _make_reopen_event("#2")
        unclaimed_response = await engine.evaluate(
            unclaimed_event,
            session_id=SESSION_ID,
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
            session_id=SESSION_ID,
            variables={
                "task_claimed": True,
                "claimed_tasks": {"uuid-1": "#1"},
                "loaded_skills": ["tasks"],
            },
        )

        assert response.decision == "allow"


class TestRequireTaskCreationSkillOnSchema:
    """Verify create_task schema lookup requires the tasks skill."""

    def test_blocks_create_task_schema_with_canonical_directive(self, db, manager) -> None:
        _sync_bundled(db)

        row = manager.get_by_name("require-task-creation-skill-on-schema")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert "get_tool_schema" in (body.when or "")
        assert "create_task" in (body.when or "")
        assert "not skill_loaded('tasks')" in (body.when or "")
        assert len(body.effects) == 1
        assert body.effects[0].type == "block"
        assert body.effects[0].reason == _skill_fetch_template("tasks")


class TestRequireTaskTransitionsSkillOnLifecycle:
    """Verify lifecycle schemas require the tasks skill."""

    def test_when_mentions_extended_lifecycle_tools(self, db, manager) -> None:
        """reopen/escalate/de_escalate should all trigger the skill directive."""
        _sync_bundled(db)

        row = manager.get_by_name("require-task-transitions-skill-on-lifecycle")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert "get_tool_schema" in (body.when or "")
        assert "reopen_task" in (body.when or "")
        assert "escalate_task" in (body.when or "")
        assert "de_escalate_task" in (body.when or "")
        assert "reject_review" in (body.when or "")
        assert "not skill_loaded('tasks')" in (body.when or "")

    def test_blocks_with_task_transitions_directive(self, db, manager) -> None:
        """The rule should block with the canonical directive."""
        _sync_bundled(db)

        row = manager.get_by_name("require-task-transitions-skill-on-lifecycle")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        block_effects = [effect for effect in body.effects if effect.type == "block"]
        set_effects = [effect for effect in body.effects if effect.type == "set_variable"]

        assert len(block_effects) == 1
        assert block_effects[0].reason == _skill_fetch_template("tasks")
        assert set_effects == []


class TestTaskLifecycleSkillGates:
    """Verify lifecycle calls require agent-loaded task skills."""

    def test_creation_gate_blocks_without_loaded_skill(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-task-creation-skill-loaded")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert "skill_loaded('tasks')" in (body.when or "")
        assert body.effects[0].reason == _skill_fetch_template("tasks")

    def test_transition_gate_blocks_without_loaded_skill(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-task-transitions-skill-loaded")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert "reopen_task" in (body.when or "")
        assert "skill_loaded('tasks')" in (body.when or "")
        assert body.effects[0].reason == _skill_fetch_template("tasks")

    @pytest.mark.asyncio
    async def test_creation_schema_gate_blocks_until_loaded(self, db, manager) -> None:
        _sync_bundled(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__get_tool_schema",
                "mcp_tool": "get_tool_schema",
                "tool_input": {"server_name": "gobby-tasks", "tool_name": "create_task"},
            },
        )

        blocked = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables={})
        allowed = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables={"loaded_skills": ["tasks"]},
        )

        assert blocked.decision == "block"
        assert "tasks" in (blocked.reason or "")
        assert allowed.decision == "allow"

    @pytest.mark.asyncio
    async def test_transition_schema_gate_blocks_until_loaded(self, db, manager) -> None:
        _sync_bundled(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__get_tool_schema",
                "mcp_tool": "get_tool_schema",
                "tool_input": {"server_name": "gobby-tasks", "tool_name": "reopen_task"},
            },
        )

        blocked = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables={})
        allowed = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables={"loaded_skills": ["tasks"]},
        )

        assert blocked.decision == "block"
        assert "tasks" in (blocked.reason or "")
        assert allowed.decision == "allow"

    @pytest.mark.asyncio
    async def test_creation_gate_blocks_normalized_call_until_loaded(self, db, manager) -> None:
        _sync_bundled(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "mcp_server": "gobby-tasks",
                "mcp_tool": "create_task",
                "tool_name": "mcp__gobby-tasks__create_task",
                "tool_input": {"title": "Work"},
            },
        )

        blocked = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables={})
        allowed = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables={"loaded_skills": ["tasks"]},
        )

        assert blocked.decision == "block"
        assert "tasks" in (blocked.reason or "")
        assert allowed.decision == "allow"

    @pytest.mark.asyncio
    async def test_creation_gate_exempts_pipeline_sessions(self, db, manager) -> None:
        """Deterministic pipeline executors cannot load skills; the gate must not fire."""
        _sync_bundled(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "mcp_server": "gobby-tasks",
                "mcp_tool": "create_task",
                "tool_name": "mcp__gobby-tasks__create_task",
                "tool_input": {"title": "Wiki research: pass"},
            },
        )

        allowed = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables={"_agent_type": "pipeline"},
        )

        assert allowed.decision == "allow"

    @pytest.mark.asyncio
    async def test_transition_gate_blocks_raw_call_until_loaded(self, db, manager) -> None:
        _sync_bundled(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "reopen_task",
                    "arguments": {"task_id": "#42"},
                },
            },
        )

        blocked = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables={})
        allowed = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables={"loaded_skills": ["tasks"]},
        )

        assert blocked.decision == "block"
        assert "tasks" in (blocked.reason or "")
        assert allowed.decision == "allow"

    @pytest.mark.asyncio
    async def test_transition_gate_exempts_pipeline_sessions(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        """Deterministic pipeline executors cannot load skills; transition gate must not fire."""
        _sync_bundled(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "reopen_task",
                    "arguments": {"task_id": "#42"},
                },
            },
        )

        allowed = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables={"_agent_type": "pipeline"},
        )

        assert allowed.decision == "allow"


def _make_reopen_event(task_id: str) -> HookEvent:
    """Create a direct MCP reopen_task before_tool event."""
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=EXTERNAL_SESSION_ID,
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
        metadata={"_platform_session_id": SESSION_ID},
    )


class TestWriteRouteParity:
    """Verify shell write routes receive the same final task-gate decision."""

    @staticmethod
    def _task_gate_decision(
        db: HubDatabase,
        manager: RuleDefinitionManager,
        tool_name: str,
        tool_input: dict[str, str],
    ) -> tuple[bool, dict[str, object]]:
        from gobby.workflows.enforcement.blocking import requires_task_for_any_touched_file
        from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

        _sync_bundled(db)
        row = manager.get_by_name("require-task-before-edit")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.when is not None

        data: dict[str, object] = {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "cwd": "/project",
            "project_root": "/project",
        }
        normalize_tool_fields(data)
        context = {
            "variables": {
                "require_task_before_edit": True,
                "task_claimed": False,
                "plan_mode": False,
            },
            "event": type("Event", (), {"data": data})(),
            "tool_input": data["tool_input"],
            "source": "claude_code",
        }
        allowed_funcs = build_condition_helpers(context=context)
        allowed_funcs["requires_task_for_any_touched_file"] = requires_task_for_any_touched_file
        decision = bool(
            SafeExpressionEvaluator(
                context=context,
                allowed_funcs=allowed_funcs,
            ).evaluate(body.when)
        )
        return decision, data

    @pytest.mark.parametrize(
        ("command", "requires_task"),
        [
            ("git apply changes.patch", True),
            ("git -C /repo apply changes.patch", True),
            ("git -c core.autocrlf=false apply changes.patch", True),
            ("patch -p1 < changes.patch", True),
            ('python -c "print(1)"', False),
            ('python3 -c "print(1)"', False),
            ('uv run python -c "print(1)"', False),
            ('uv run python3 -c "print(1)"', False),
            ('uv run --with rich python -c "print(1)"', False),
            ('node -e "console.log(1)"', True),
            ('node --eval "console.log(1)"', True),
            ('ruby -e "puts 1"', True),
            ("git apply --check changes.patch", False),
            ("git -C /repo apply --check changes.patch", False),
            ("git apply --stat changes.patch", False),
            ("git apply --numstat changes.patch", False),
            ("patch --dry-run -p1 < changes.patch", False),
        ],
    )
    def test_patch_and_inline_interpreter_routes_gate_consistently(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
        command: str,
        requires_task: bool,
    ) -> None:
        decision, data = self._task_gate_decision(
            db,
            manager,
            "Bash",
            {"command": command},
        )

        assert decision is requires_task
        if requires_task:
            assert data["canonical_tool_kind"] == "write"
            assert data["canonical_repo_mutation"] is True
            assert data.get("canonical_file_paths", []) == []
        else:
            assert data.get("canonical_repo_mutation", False) is False

    @pytest.mark.parametrize(
        ("relative_path", "requires_task"),
        [
            (".gobby/plans/web-styling-consolidation-phase-2.md", False),
            ("src/main.py", True),
        ],
    )
    def test_write_and_bash_redirect_gate_the_same_target_consistently(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
        relative_path: str,
        requires_task: bool,
    ) -> None:
        tool_payloads: list[tuple[str, dict[str, str]]] = [
            ("Write", {"file_path": f"/project/{relative_path}"}),
            ("Bash", {"command": f"printf content > {relative_path}"}),
        ]
        decisions = [
            self._task_gate_decision(db, manager, tool_name, tool_input)[0]
            for tool_name, tool_input in tool_payloads
        ]

        assert decisions == [requires_task, requires_task]

    @pytest.mark.parametrize(
        ("command", "expected_kind", "repo_mutation", "requires_task"),
        [
            (
                "cat pyproject.toml | python3 -c 'import json, sys; print(json.load(sys.stdin))'",
                "read",
                False,
                False,
            ),
            (
                "curl -fsSL https://example.test/data.json -o {scratchpad}/data.json",
                "write",
                False,
                False,
            ),
            ("printf content > {scratchpad}/output.txt", "write", False, False),
            ("cat pyproject.toml", "read", False, False),
            ("printf content > src/main.py", "write", True, True),
            ("printf content | tee src/main.py", "write", True, True),
            ("cat <<'EOF' > src/main.py\ncontent\nEOF", "write", True, True),
            ("sed -i 's/old/new/' src/main.py", "write", True, True),
            ("cat pyproject.toml && touch src/main.py", "write", True, True),
            (
                "curl -fsSL https://example.test/data.json -o src/data.json",
                "write",
                True,
                True,
            ),
            ("curl -fsSL https://example.test/data.json", "execute", False, False),
            ("curl -fsSLO https://example.test/data.json", "write", True, True),
            (
                "printf content | python3 -c "
                '\'import sys; open("src/main.py", "w").write(sys.stdin.read())\'',
                "write",
                True,
                True,
            ),
            ('python3 -c "print(1)"', "execute", False, False),
            ('printf content | python3 -c "unknown()"', "execute", False, False),
            (
                "printf content | python3 -c "
                '\'import json, sys; json = sys.modules["os"]; '
                'json.loads = json.remove; json.loads("src/main.py")\'',
                "write",
                True,
                True,
            ),
            (
                'python3 -c "unknown()" && curl -o '
                "{scratchpad}/data.json https://example.test/data.json",
                "write",
                False,
                False,
            ),
            (
                "python3 - <<'PY'\nopen('src/main.py', 'w').write('content')\nPY",
                "write",
                True,
                True,
            ),
        ],
    )
    def test_shell_command_task_gate_classification_table(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
        command: str,
        expected_kind: str,
        repo_mutation: bool,
        requires_task: bool,
        tmp_path: Path,
    ) -> None:
        command = command.format(scratchpad=tmp_path / "gobby-agent-scratchpad-session")
        decision, data = self._task_gate_decision(
            db,
            manager,
            "Bash",
            {"command": command},
        )

        assert data["canonical_tool_kind"] == expected_kind
        assert data.get("canonical_repo_mutation", False) is repo_mutation
        assert decision is requires_task
