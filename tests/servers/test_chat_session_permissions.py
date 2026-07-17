"""Tests for ChatSession permissions and tool approval logic."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from gobby.servers.chat_session import ChatSession
from tests._timing import wait_for_async_condition

pytestmark = pytest.mark.unit


@pytest.fixture
def session() -> ChatSession:
    sess = ChatSession(conversation_id="test-perms")
    sess.chat_mode = "normal"
    sess._tool_approval_config = None
    sess._plan_approved = False
    return sess


class TestCanUseTool:
    @pytest.mark.asyncio
    async def test_enter_plan_mode(self, session: ChatSession) -> None:
        """EnterPlanMode switches chat_mode to plan and returns Allow."""
        mock_cb = AsyncMock()
        session._on_mode_changed = mock_cb

        result = await session._can_use_tool(
            "EnterPlanMode", {"foo": "bar"}, ToolPermissionContext(tool_use_id="tool-test")
        )
        assert isinstance(result, PermissionResultAllow)
        assert session.chat_mode == "plan"
        mock_cb.assert_awaited_once_with("plan", "agent_requested")

    @pytest.mark.asyncio
    async def test_exit_plan_mode_no_content(self, session: ChatSession) -> None:
        """ExitPlanMode denies when neither tool input nor a plan file has content."""
        session.set_chat_mode("plan")
        with patch.object(session, "_read_plan_file", return_value=None):
            result = await session._can_use_tool(
                "ExitPlanMode", {}, ToolPermissionContext(tool_use_id="tool-test")
            )
            assert isinstance(result, PermissionResultDeny)
            assert "No plan content found" in result.message

    @pytest.mark.asyncio
    async def test_exit_plan_mode_input_plan_broadcasts_and_approves(
        self, session: ChatSession
    ) -> None:
        """Plan sourced from the ExitPlanMode tool input (no file) broadcasts
        plan_pending_approval exactly once, blocks, then approves."""
        session.set_chat_mode("plan")
        on_plan_ready = AsyncMock()
        session._on_plan_ready = on_plan_ready
        session._on_mode_changed = AsyncMock()
        with patch.object(session, "_read_plan_file", return_value=None):
            task = asyncio.create_task(
                session._can_use_tool(
                    "ExitPlanMode",
                    {"plan": "# Plan\nDo the thing"},
                    ToolPermissionContext(tool_use_id="tool-test"),
                )
            )
            await wait_for_async_condition(
                lambda: session.has_pending_plan, description="pending plan"
            )
            on_plan_ready.assert_awaited_once()
            assert on_plan_ready.await_args.args[0] == "# Plan\nDo the thing"
            assert session._plan_broadcast_sent is True
            session._pending_post_plan_mode = "bypass"
            session.provide_plan_decision("tool-test", "approve")
            result = await task

        assert isinstance(result, PermissionResultAllow)
        assert session.chat_mode == "bypass"
        assert session._plan_approved is True
        assert session._plan_broadcast_sent is False
        session._on_mode_changed.assert_awaited_once_with("bypass", "plan_approved")

    @pytest.mark.asyncio
    async def test_exit_plan_mode_input_plan_request_changes_resets_flag(
        self, session: ChatSession
    ) -> None:
        """Request-changes denies with feedback and resets the broadcast flag so
        the agent's revised plan re-broadcasts on the next ExitPlanMode."""
        session.set_chat_mode("plan")
        session._on_plan_ready = AsyncMock()
        session._on_mode_changed = AsyncMock()
        session.set_plan_feedback("tighten scope")
        with patch.object(session, "_read_plan_file", return_value=None):
            task = asyncio.create_task(
                session._can_use_tool(
                    "ExitPlanMode",
                    {"plan": "draft"},
                    ToolPermissionContext(tool_use_id="tool-test"),
                )
            )
            await wait_for_async_condition(
                lambda: session.has_pending_plan, description="pending plan"
            )
            session.provide_plan_decision("tool-test", "request_changes")
            result = await task

        assert isinstance(result, PermissionResultDeny)
        assert "tighten scope" in result.message
        assert session.chat_mode == "plan"
        assert session._plan_broadcast_sent is False
        session._on_mode_changed.assert_awaited_once_with("plan", "plan_changes_requested")

    @pytest.mark.asyncio
    async def test_exit_plan_mode_dedupes_after_file_write_broadcast(
        self, session: ChatSession
    ) -> None:
        """If the file-write PostToolUse hook already broadcast this cycle,
        ExitPlanMode does not broadcast a duplicate."""
        session.set_chat_mode("plan")
        session._plan_broadcast_sent = True  # prior file-write broadcast
        session._pending_plan_content = "input plan"
        on_plan_ready = AsyncMock()
        session._on_plan_ready = on_plan_ready
        session._on_mode_changed = AsyncMock()
        with patch.object(session, "_read_plan_file", return_value="file plan"):
            task = asyncio.create_task(
                session._can_use_tool(
                    "ExitPlanMode",
                    {"plan": "input plan"},
                    ToolPermissionContext(tool_use_id="tool-test"),
                )
            )
            await wait_for_async_condition(
                lambda: session.has_pending_plan, description="pending plan"
            )
            session._pending_post_plan_mode = "bypass"
            session.provide_plan_decision("tool-test", "approve")
            result = await task

        # No duplicate broadcast, but the gate still resolves normally.
        on_plan_ready.assert_not_awaited()
        assert isinstance(result, PermissionResultAllow)
        assert session.chat_mode == "bypass"
        assert session._plan_approved is True

    @pytest.mark.asyncio
    async def test_exit_plan_mode_rebroadcasts_revised_plan(self, session: ChatSession) -> None:
        """Changed plan content resets de-dupe and broadcasts the revised plan."""
        session.set_chat_mode("plan")
        session._plan_broadcast_sent = True
        session._pending_plan_content = "old plan"
        on_plan_ready = AsyncMock()
        session._on_plan_ready = on_plan_ready
        session._on_mode_changed = AsyncMock()
        with patch.object(session, "_read_plan_file", return_value=None):
            task = asyncio.create_task(
                session._can_use_tool(
                    "ExitPlanMode",
                    {"plan": "revised plan"},
                    ToolPermissionContext(tool_use_id="tool-test"),
                )
            )
            await wait_for_async_condition(
                lambda: session.has_pending_plan, description="pending plan"
            )
            session._pending_post_plan_mode = "bypass"
            session.provide_plan_decision("tool-test", "approve")
            result = await task

        on_plan_ready.assert_awaited_once()
        assert on_plan_ready.await_args.args[0] == "revised plan"
        assert isinstance(result, PermissionResultAllow)
        assert session.chat_mode == "bypass"

    @pytest.mark.asyncio
    async def test_exit_plan_mode_already_approved(self, session: ChatSession) -> None:
        """ExitPlanMode returns Allow immediately if already approved."""
        session.set_chat_mode("plan")
        session._plan_approved = True
        session._plan_file_path = "some_file.md"

        result = await session._can_use_tool(
            "ExitPlanMode", {}, ToolPermissionContext(tool_use_id="tool-test")
        )
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_exit_plan_mode_blocking_approval(self, session: ChatSession) -> None:
        """ExitPlanMode blocks until user approves."""
        session.set_chat_mode("plan")
        session._plan_file_path = "p.md"
        session._last_plan_content = "draft plan"
        session._on_mode_changed = AsyncMock()

        task = asyncio.create_task(
            session._can_use_tool(
                "ExitPlanMode", {}, ToolPermissionContext(tool_use_id="tool-test")
            )
        )
        await wait_for_async_condition(lambda: session.has_pending_plan, description="pending plan")
        session._pending_post_plan_mode = "bypass"
        session.provide_plan_decision("tool-test", "approve")
        result = await task

        assert isinstance(result, PermissionResultAllow)
        assert session.chat_mode == "bypass"
        assert session._plan_approved is True
        session._on_mode_changed.assert_awaited_once_with("bypass", "plan_approved")

    @pytest.mark.asyncio
    async def test_exit_plan_mode_blocking_rejection(self, session: ChatSession) -> None:
        """ExitPlanMode blocks until user requests changes."""
        session.set_chat_mode("plan")
        session._plan_file_path = "p.md"
        session._last_plan_content = "draft plan"
        session.set_plan_feedback("too complex")

        task = asyncio.create_task(
            session._can_use_tool(
                "ExitPlanMode", {}, ToolPermissionContext(tool_use_id="tool-test")
            )
        )
        await wait_for_async_condition(lambda: session.has_pending_plan, description="pending plan")
        session.provide_plan_decision("tool-test", "request_changes")
        result = await task

        assert isinstance(result, PermissionResultDeny)
        assert "User requested changes" in result.message
        assert "too complex" in result.message
        assert session.chat_mode == "plan"  # Should stay in plan mode

    async def test_exit_plan_mode_timeout_stops_turn_and_reconciles(
        self, session: ChatSession
    ) -> None:
        session.set_chat_mode("plan")
        session._on_plan_ready = AsyncMock()
        session._on_mode_changed = AsyncMock()

        with (
            patch(
                "gobby.servers.chat_session_permissions.PLAN_DECISION_TIMEOUT_SECONDS",
                0.01,
            ),
            patch.object(session, "interrupt", new_callable=AsyncMock) as interrupt,
        ):
            result = await session._can_use_tool(
                "ExitPlanMode",
                {"plan": "# Plan\nDo the thing"},
                ToolPermissionContext(tool_use_id="tool-timeout"),
            )

        assert isinstance(result, PermissionResultDeny)
        assert result.message == "Plan approval timed out; the turn was stopped."
        interrupt.assert_awaited_once()
        session._on_mode_changed.assert_awaited_once_with("plan", "plan_approval_timed_out")
        assert session.has_pending_plan is False
        assert session._pending_plan_content is None
        assert session._plan_broadcast_sent is False

    async def test_interrupt_releases_pending_exit_plan_mode(self, session: ChatSession) -> None:
        session.set_chat_mode("plan")
        task = asyncio.create_task(
            session._can_use_tool(
                "ExitPlanMode",
                {"plan": "# Plan\nDo the thing"},
                ToolPermissionContext(tool_use_id="tool-interrupt"),
            )
        )
        await wait_for_async_condition(lambda: session.has_pending_plan, description="pending plan")

        await session.interrupt()
        result = await asyncio.wait_for(task, timeout=0.2)

        assert isinstance(result, PermissionResultDeny)
        assert session.has_pending_plan is False

    @pytest.mark.asyncio
    async def test_plan_mode_blocks_writes(self, session: ChatSession) -> None:
        """Write tools should be blocked in plan mode if unapproved."""
        session.set_chat_mode("plan")
        result = await session._can_use_tool(
            "Write", {"file_path": "main.py"}, ToolPermissionContext()
        )
        assert isinstance(result, PermissionResultDeny)
        assert "Plan mode is active" in result.message

    @pytest.mark.asyncio
    async def test_plan_mode_allows_plan_file_writes(
        self, session: ChatSession, tmp_path: Path
    ) -> None:
        """Write tools writing to plan files are allowed in plan mode."""
        session.project_path = str(tmp_path)
        session.set_chat_mode("plan")
        result = await session._can_use_tool(
            "Write", {"file_path": ".gobby/plans/my_plan.md"}, ToolPermissionContext()
        )
        assert isinstance(result, PermissionResultAllow)
        assert session._plan_file_path == ".gobby/plans/my_plan.md"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool_name", "file_path"),
        [
            ("Write", ".claude/plans/external.md"),
            ("Edit", "../../.gobby/plans/external.md"),
        ],
    )
    async def test_plan_mode_blocks_external_plan_file_writes(
        self,
        session: ChatSession,
        tmp_path: Path,
        tool_name: str,
        file_path: str,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        session.project_path = str(repo)
        candidate = str(tmp_path / file_path) if file_path.startswith(".claude") else file_path
        session.set_chat_mode("plan")

        result = await session._can_use_tool(
            tool_name,
            {"file_path": candidate},
            ToolPermissionContext(tool_use_id="tool-test"),
        )

        assert isinstance(result, PermissionResultDeny)
        assert "Plan mode is active" in result.message

    @pytest.mark.asyncio
    async def test_plan_mode_blocks_dangerous_bash(self, session: ChatSession) -> None:
        """Bash write tools should be blocked in plan mode."""
        session.set_chat_mode("plan")
        result = await session._can_use_tool(
            "Bash", {"command": "rm -rf /"}, ToolPermissionContext()
        )
        assert isinstance(result, PermissionResultDeny)
        assert "Plan mode is active" in result.message

    @pytest.mark.asyncio
    async def test_plan_mode_blocks_dangerous_exec_command(self, session: ChatSession) -> None:
        """Shell aliases should be blocked in plan mode like Bash."""
        session.set_chat_mode("plan")
        result = await session._can_use_tool(
            "exec_command", {"command": "rm -rf /"}, ToolPermissionContext()
        )
        assert isinstance(result, PermissionResultDeny)
        assert "Plan mode is active" in result.message

    @pytest.mark.parametrize(
        "command",
        [
            "python -c \"open('x', 'w').write('changed')\"",
            "printf changed | tee x",
        ],
    )
    async def test_plan_mode_blocks_shell_commands_outside_read_only_allowlist(
        self, session: ChatSession, command: str
    ) -> None:
        session.set_chat_mode("plan")

        result = await session._can_use_tool("Bash", {"command": command}, ToolPermissionContext())

        assert isinstance(result, PermissionResultDeny)
        assert "Plan mode is active" in result.message

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ["exec_command", "run_shell_command"])
    async def test_plan_mode_allows_gcode_shell_aliases(
        self, session: ChatSession, tool_name: str
    ) -> None:
        session.set_chat_mode("plan")
        result = await session._can_use_tool(
            tool_name,
            {"command": 'gcode search "ChatSessionPermissionsMixin"'},
            ToolPermissionContext(tool_use_id="tool-test"),
        )
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_plan_mode_blocks_gcode_shell_redirection(self, session: ChatSession) -> None:
        session.set_chat_mode("plan")
        result = await session._can_use_tool(
            "run_shell_command",
            {"command": 'gcode search "ChatSession" > notes.txt'},
            ToolPermissionContext(tool_use_id="tool-test"),
        )
        assert isinstance(result, PermissionResultDeny)
        assert "Plan mode is active" in result.message

    @pytest.mark.asyncio
    async def test_pre_tool_hook_blocks(self, session: ChatSession) -> None:
        """Session lifecycle can block a tool."""
        mock_cb = AsyncMock()
        mock_cb.return_value = {"decision": "block", "reason": "No go"}
        session._on_pre_tool = mock_cb

        result = await session._can_use_tool("Read", {}, ToolPermissionContext())
        assert isinstance(result, PermissionResultDeny)
        assert result.message == "No go"

    @pytest.mark.asyncio
    async def test_safe_mcp_proxy_discovery_skips_tool_approval_in_plan_mode(
        self, session: ChatSession
    ) -> None:
        """Registry discovery should stay available even when approval policy is ask."""
        session.chat_mode = "plan"
        session._plan_approved = True
        config = MagicMock()
        config.enabled = True
        config.default_policy = "ask"
        config.policies = []
        session._tool_approval_config = config

        result = await session._can_use_tool(
            "mcp__gobby__list_mcp_servers",
            {},
            ToolPermissionContext(tool_use_id="tool-test"),
        )

        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_safe_artifacts_calls_skip_tool_approval(self, session: ChatSession) -> None:
        """Read-only artifact display tools should be usable without prompting for approval."""
        session.chat_mode = "normal"
        config = MagicMock()
        config.enabled = True
        config.default_policy = "ask"
        config.policies = []
        session._tool_approval_config = config

        result = await session._can_use_tool(
            "mcp__gobby__call_tool",
            {
                "server_name": "gobby-artifacts",
                "tool_name": "show_file",
            },
            ToolPermissionContext(tool_use_id="tool-test"),
        )

        assert isinstance(result, PermissionResultAllow)

    async def test_plan_mode_allows_read_only_mcp_call(self, session: ChatSession) -> None:
        session.set_chat_mode("plan")

        result = await session._can_use_tool(
            "mcp__gobby__call_tool",
            {"server_name": "external", "tool_name": "read_file", "arguments": {}},
            ToolPermissionContext(tool_use_id="tool-test"),
        )

        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.parametrize("tool_name", ["create_file", "run"])
    async def test_plan_mode_blocks_mcp_calls_outside_read_only_allowlist(
        self, session: ChatSession, tool_name: str
    ) -> None:
        session.set_chat_mode("plan")

        result = await session._can_use_tool(
            "mcp__gobby__call_tool",
            {"server_name": "external", "tool_name": tool_name, "arguments": {}},
            ToolPermissionContext(tool_use_id="tool-test"),
        )

        assert isinstance(result, PermissionResultDeny)
        assert "Plan mode is active" in result.message


class TestNeedsToolApproval:
    def test_bypass_mode(self, session: ChatSession) -> None:
        session.chat_mode = "bypass"
        assert not session._needs_tool_approval("Write", {"file_path": "main.py"})

    def test_accept_edits_mode(self, session: ChatSession) -> None:
        session.chat_mode = "accept_edits"
        assert session._needs_tool_approval("Write", {"file_path": "main.py"})
        assert session._needs_tool_approval("Edit", {"file_path": "main.py"})
        assert session._needs_tool_approval("NotebookEdit", {"notebook_path": "main.ipynb"})
        assert session._needs_tool_approval("exec_command", {"command": "pytest"})
        assert not session._needs_tool_approval("Read", {"file_path": "main.py"})
        assert not session._needs_tool_approval("mcp__gobby__list_tools", {})

    def test_normal_mode_default_read_allowlist(self, session: ChatSession) -> None:
        session.chat_mode = "normal"
        assert not session._needs_tool_approval("Read", {"file_path": "main.py"})
        assert not session._needs_tool_approval("Glob", {"pattern": "**/*.py"})
        assert not session._needs_tool_approval("Grep", {"pattern": "ChatMode"})
        assert not session._needs_tool_approval("Ls", {"path": "."})
        assert session._needs_tool_approval("Write", {"file_path": "main.py"})

    def test_normal_mode_shared_allowlists(self, session: ChatSession) -> None:
        session.chat_mode = "normal"
        session._approved_tools = {"tool:Write", "mcp:third-party:search_docs"}
        assert not session._needs_tool_approval("Write", {"file_path": "main.py"})
        assert not session._needs_tool_approval(
            "mcp__third-party__search_docs",
            {},
        )
        assert not session._needs_tool_approval("mcp__gobby__do_thing", {})
        assert session._needs_tool_approval("mcp__other__do_thing", {})

    def test_normal_mode_gcode_skips_approval(self, session: ChatSession) -> None:
        session.chat_mode = "normal"

        assert not session._needs_tool_approval(
            "Bash",
            {"command": 'gcode search "ChatSessionPermissionsMixin"'},
        )

    def test_normal_mode_gcode_with_redirection_requires_approval(
        self, session: ChatSession
    ) -> None:
        session.chat_mode = "normal"

        assert session._needs_tool_approval(
            "Bash",
            {"command": 'gcode search "ChatSession" > notes.txt'},
        )

    def test_normal_mode_gcode_with_shell_separator_requires_approval(
        self, session: ChatSession
    ) -> None:
        session.chat_mode = "normal"

        assert session._needs_tool_approval(
            "Bash",
            {"command": 'gcode search "ChatSession"; echo done'},
        )


class TestDangerousPatterns:
    def test_is_write_bash(self, session: ChatSession) -> None:
        assert session._is_write_bash({"command": "echo hello > test.txt"})
        assert session._is_write_bash({"command": "npm install"})
        assert not session._is_write_bash({"command": "pytest"})
        assert not session._is_write_bash({"command": "cat test.txt"})

    def test_is_write_mcp_call(self, session: ChatSession) -> None:
        assert not session._is_write_mcp_call({"server_name": "x", "tool_name": "read_file"})
        assert not session._is_write_mcp_call({"server_name": "x", "tool_name": "list_dirs"})
        assert not session._is_write_mcp_call(
            {"server_name": "gobby-artifacts", "tool_name": "show_file"}
        )
        assert session._is_write_mcp_call({"server_name": "x", "tool_name": "create_file"})
        assert session._is_write_mcp_call({})  # No tool name -> True by default


class TestWaitForToolApproval:
    @pytest.mark.asyncio
    async def test_wait_for_tool_approval_approve(self, session: ChatSession) -> None:
        session._tool_approval_callback = AsyncMock()

        task = asyncio.create_task(
            session._wait_for_tool_approval("tool-test", "Bash", {"command": "ls"})
        )
        await wait_for_async_condition(
            lambda: session.has_pending_approval, description="pending approval"
        )
        session.provide_approval("tool-test", "approve")
        result = await task

        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input == {"command": "ls"}

    @pytest.mark.asyncio
    async def test_concurrent_approvals_resolve_only_matching_tool_use_id(
        self, session: ChatSession
    ) -> None:
        session._tool_approval_callback = AsyncMock()

        first = asyncio.create_task(
            session._wait_for_tool_approval("tool-a", "Bash", {"command": "first"})
        )
        second = asyncio.create_task(
            session._wait_for_tool_approval("tool-b", "Bash", {"command": "second"})
        )
        await wait_for_async_condition(
            lambda: len(session._pending_approvals) == 2,
            description="two pending approvals",
        )

        assert session.provide_approval("tool-a", "approve") is True
        first_result = await first
        assert isinstance(first_result, PermissionResultAllow)
        assert first_result.updated_input == {"command": "first"}
        assert second.done() is False

        assert session.provide_approval("tool-b", "reject") is True
        second_result = await second
        assert isinstance(second_result, PermissionResultDeny)

    @pytest.mark.asyncio
    async def test_wait_for_tool_approval_reject(self, session: ChatSession) -> None:
        session._tool_approval_callback = AsyncMock()

        task = asyncio.create_task(
            session._wait_for_tool_approval("tool-test", "Bash", {"command": "ls"})
        )
        await wait_for_async_condition(
            lambda: session.has_pending_approval, description="pending approval"
        )
        session.provide_approval("tool-test", "reject")
        result = await task

        assert isinstance(result, PermissionResultDeny)

    @pytest.mark.asyncio
    async def test_wait_for_tool_approval_approve_always(self, session: ChatSession) -> None:
        session._tool_approval_callback = AsyncMock()
        session._on_approved_tools_persist = MagicMock()

        task = asyncio.create_task(
            session._wait_for_tool_approval("tool-test", "Bash", {"command": "ls"})
        )
        await wait_for_async_condition(
            lambda: session.has_pending_approval, description="pending approval"
        )
        session.provide_approval("tool-test", "approve_always")
        result = await task

        assert isinstance(result, PermissionResultAllow)
        assert "tool:Bash" in session._approved_tools
        session._on_approved_tools_persist.assert_called_once_with({"tool:Bash"})


class TestConsumePlanModeContext:
    def test_consume_mode_context_act(self, session: ChatSession) -> None:
        session.chat_mode = "normal"
        context = session._consume_plan_mode_context()
        assert context is not None
        assert 'status="act"' in context

    def test_consume_mode_context_yolo(self, session: ChatSession) -> None:
        session.chat_mode = "bypass"
        context = session._consume_plan_mode_context()
        assert context is not None
        assert 'status="yolo"' in context
        assert "YOLO MODE" in context

    def test_consume_plan_mode_approved(self, session: ChatSession) -> None:
        session.chat_mode = "plan"
        session._plan_approved = True
        context = session._consume_plan_mode_context()
        assert context is not None
        assert 'status="approved"' in context

    def test_consume_plan_mode_feedback(self, session: ChatSession) -> None:
        session.chat_mode = "plan"
        session._plan_feedback = "Do it better"
        context = session._consume_plan_mode_context()

        assert context is not None
        assert "Do it better" in context
        assert session._plan_feedback is None  # Should be cleared

    def test_consume_plan_mode_context_mentions_gcode(self, session: ChatSession) -> None:
        session.chat_mode = "plan"
        context = session._consume_plan_mode_context()

        assert context is not None
        assert "gcode outline/search/symbol" in context


class TestStartPlanPermissionMode:
    @pytest.mark.asyncio
    async def test_start_in_plan_mode_pushes_native_plan_permission(self) -> None:
        """A session that begins in plan mode connects the SDK with native
        permission_mode 'plan' so Claude emits ExitPlanMode (the §1 base trigger)."""
        session = ChatSession(conversation_id="test-start-plan")
        session.chat_mode = "plan"
        session.system_prompt_override = "sys"  # skip prompt loading
        captured: dict[str, object] = {}

        class _FakeClient:
            def __init__(self, options: object) -> None:
                captured["options"] = options

            async def connect(self) -> None:
                return None

        with (
            patch(
                "gobby.servers.chat_session._find_cli_path",
                return_value="/usr/bin/claude",
            ),
            patch(
                "gobby.servers.chat_session.materialize_claude_settings_async",
                new=AsyncMock(return_value="/tmp/settings.json"),
            ),
            patch("gobby.servers.chat_session.ClaudeSDKClient", _FakeClient),
            patch.object(
                ChatSession,
                "_resolve_requested_model",
                new=AsyncMock(return_value="claude-x"),
            ),
        ):
            await session.start()

        options = captured["options"]
        assert options.permission_mode == "plan"
        # The gate callback that intercepts ExitPlanMode must be wired in.
        assert options.can_use_tool == session._can_use_tool
        assert session._connected is True

    @pytest.mark.asyncio
    async def test_start_in_normal_mode_pushes_default_permission(self) -> None:
        """A non-plan session maps to the SDK 'default' permission mode."""
        session = ChatSession(conversation_id="test-start-normal")
        session.chat_mode = "normal"
        session.system_prompt_override = "sys"
        captured: dict[str, object] = {}

        class _FakeClient:
            def __init__(self, options: object) -> None:
                captured["options"] = options

            async def connect(self) -> None:
                return None

        with (
            patch(
                "gobby.servers.chat_session._find_cli_path",
                return_value="/usr/bin/claude",
            ),
            patch(
                "gobby.servers.chat_session.materialize_claude_settings_async",
                new=AsyncMock(return_value="/tmp/settings.json"),
            ),
            patch("gobby.servers.chat_session.ClaudeSDKClient", _FakeClient),
            patch.object(
                ChatSession,
                "_resolve_requested_model",
                new=AsyncMock(return_value="claude-x"),
            ),
        ):
            await session.start()

        options = captured["options"]
        assert options.permission_mode == "default"
        assert options.can_use_tool == session._can_use_tool
        assert session._connected is True
