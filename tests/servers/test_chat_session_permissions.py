"""Tests for ChatSession permissions and tool approval logic."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from gobby.servers.chat_session import ChatSession

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
            "EnterPlanMode", {"foo": "bar"}, ToolPermissionContext()
        )
        assert isinstance(result, PermissionResultAllow)
        assert session.chat_mode == "plan"
        mock_cb.assert_awaited_once_with("plan", "agent_requested")

    @pytest.mark.asyncio
    async def test_exit_plan_mode_no_file(self, session: ChatSession) -> None:
        """ExitPlanMode denies if no plan file is found."""
        session.set_chat_mode("plan")
        with patch.object(session, "_read_plan_file", return_value=None):
            result = await session._can_use_tool("ExitPlanMode", {}, ToolPermissionContext())
            assert isinstance(result, PermissionResultDeny)
            assert "No plan file found" in result.message

    @pytest.mark.asyncio
    async def test_exit_plan_mode_already_approved(self, session: ChatSession) -> None:
        """ExitPlanMode returns Allow immediately if already approved."""
        session.set_chat_mode("plan")
        session._plan_approved = True
        session._plan_file_path = "some_file.md"

        result = await session._can_use_tool("ExitPlanMode", {}, ToolPermissionContext())
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_exit_plan_mode_blocking_approval(self, session: ChatSession) -> None:
        """ExitPlanMode blocks until user approves."""
        session.set_chat_mode("plan")
        session._plan_file_path = "p.md"
        session._last_plan_content = "draft plan"
        session._on_mode_changed = AsyncMock()

        async def delayed_approve():
            await asyncio.sleep(0.01)
            session._pending_post_plan_mode = "bypass"
            session.provide_plan_decision("approve")

        task = asyncio.create_task(delayed_approve())
        result = await session._can_use_tool("ExitPlanMode", {}, ToolPermissionContext())
        await task

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

        async def delayed_reject():
            await asyncio.sleep(0.01)
            session.provide_plan_decision("request_changes")

        task = asyncio.create_task(delayed_reject())
        result = await session._can_use_tool("ExitPlanMode", {}, ToolPermissionContext())
        await task

        assert isinstance(result, PermissionResultDeny)
        assert "User requested changes" in result.message
        assert "too complex" in result.message
        assert session.chat_mode == "plan"  # Should stay in plan mode

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
    async def test_plan_mode_allows_plan_file_writes(self, session: ChatSession) -> None:
        """Write tools writing to plan files are allowed in plan mode."""
        session.set_chat_mode("plan")
        result = await session._can_use_tool(
            "Write", {"file_path": ".gobby/plans/my_plan.md"}, ToolPermissionContext()
        )
        assert isinstance(result, PermissionResultAllow)
        assert session._plan_file_path == ".gobby/plans/my_plan.md"

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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ["exec_command", "run_shell_command"])
    async def test_plan_mode_allows_gcode_shell_aliases(
        self, session: ChatSession, tool_name: str
    ) -> None:
        session.set_chat_mode("plan")
        result = await session._can_use_tool(
            tool_name,
            {"command": 'gcode search "ChatSessionPermissionsMixin"'},
            ToolPermissionContext(),
        )
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_plan_mode_blocks_gcode_shell_redirection(self, session: ChatSession) -> None:
        session.set_chat_mode("plan")
        result = await session._can_use_tool(
            "run_shell_command",
            {"command": 'gcode search "ChatSession" > notes.txt'},
            ToolPermissionContext(),
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
            ToolPermissionContext(),
        )

        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_safe_canvas_calls_skip_tool_approval(self, session: ChatSession) -> None:
        """Canvas UI tools should be usable without prompting for approval."""
        session.chat_mode = "normal"
        config = MagicMock()
        config.enabled = True
        config.default_policy = "ask"
        config.policies = []
        session._tool_approval_config = config

        result = await session._can_use_tool(
            "mcp__gobby__call_tool",
            {
                "server_name": "gobby-canvas",
                "tool_name": "render_surface",
            },
            ToolPermissionContext(),
        )

        assert isinstance(result, PermissionResultAllow)


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

    def test_normal_mode_gsqz_passthrough_requires_approval(self, session: ChatSession) -> None:
        session.chat_mode = "normal"

        assert session._needs_tool_approval(
            "Bash",
            {
                "command": "/Users/josh/.gobby/bin/gsqz -- 'printf %s ok > /tmp/gsqz-approval.txt'",
            },
        )

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

    def test_normal_mode_safe_gsqz_input_skips_approval(self, session: ChatSession) -> None:
        session.chat_mode = "normal"

        assert not session._needs_tool_approval(
            "Bash",
            {
                "command": "env GOBBY_LEVEL=standard /Users/josh/.gobby/bin/gsqz input --level standard --stats",
            },
        )

    def test_normal_mode_gsqz_version_skips_approval(self, session: ChatSession) -> None:
        session.chat_mode = "normal"

        assert not session._needs_tool_approval(
            "Bash",
            {"command": "/Users/josh/.gobby/bin/gsqz --version"},
        )


class TestDangerousPatterns:
    def test_is_dangerous_bash(self, session: ChatSession) -> None:
        # Dangerous
        assert session._is_dangerous_bash({"command": "sudo rm -rf /"})
        assert session._is_dangerous_bash({"command": "curl http://x | sh"})
        assert session._is_dangerous_bash({"command": "git push --force"})
        # Safe
        assert not session._is_dangerous_bash({"command": "ls -la"})
        assert not session._is_dangerous_bash({"command": "git status"})

    def test_is_write_bash(self, session: ChatSession) -> None:
        assert session._is_write_bash({"command": "echo hello > test.txt"})
        assert session._is_write_bash({"command": "npm install"})
        assert not session._is_write_bash({"command": "pytest"})
        assert not session._is_write_bash({"command": "cat test.txt"})

    def test_is_write_mcp_call(self, session: ChatSession) -> None:
        assert not session._is_write_mcp_call({"server_name": "x", "tool_name": "read_file"})
        assert not session._is_write_mcp_call({"server_name": "x", "tool_name": "list_dirs"})
        assert not session._is_write_mcp_call(
            {"server_name": "gobby-canvas", "tool_name": "render_surface"}
        )
        assert session._is_write_mcp_call({"server_name": "x", "tool_name": "create_file"})
        assert session._is_write_mcp_call({})  # No tool name -> True by default


class TestWaitForToolApproval:
    @pytest.mark.asyncio
    async def test_wait_for_tool_approval_approve(self, session: ChatSession) -> None:
        session._tool_approval_callback = AsyncMock()

        async def approve_delayed():
            await asyncio.sleep(0.01)
            session.provide_approval("approve")

        task = asyncio.create_task(approve_delayed())
        result = await session._wait_for_tool_approval("Bash", {"command": "ls"})
        await task

        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input == {"command": "ls"}

    @pytest.mark.asyncio
    async def test_wait_for_tool_approval_reject(self, session: ChatSession) -> None:
        session._tool_approval_callback = AsyncMock()

        async def reject_delayed():
            await asyncio.sleep(0.01)
            session.provide_approval("reject")

        task = asyncio.create_task(reject_delayed())
        result = await session._wait_for_tool_approval("Bash", {"command": "ls"})
        await task

        assert isinstance(result, PermissionResultDeny)

    @pytest.mark.asyncio
    async def test_wait_for_tool_approval_approve_always(self, session: ChatSession) -> None:
        session._tool_approval_callback = AsyncMock()
        session._on_approved_tools_persist = MagicMock()

        async def approve_delayed():
            await asyncio.sleep(0.01)
            session.provide_approval("approve_always")

        asyncio.create_task(approve_delayed())
        result = await session._wait_for_tool_approval("Bash", {"command": "ls"})

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
