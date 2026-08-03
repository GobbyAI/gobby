"""
E2E tests for inter-agent messaging via MCP tools.

Tests verify:
1. P2P message exchange works via gobby-agents MCP tools
2. Messages are persisted and deliverable
3. Message delivery status is tracked

Test scenario:
1. Start daemon
2. Register parent and child sessions
3. Parent sends message via send_message
4. Child reads its inbox via get_inter_session_messages
5. Child responds via send_message
6. Parent receives response
"""

import uuid

import pytest

from tests.e2e.conftest import (
    CLIEventSimulator,
    DaemonInstance,
    MCPTestClient,
)

pytestmark = pytest.mark.e2e


def unwrap_result(result: dict) -> dict:
    """Unwrap MCP tool call result from wrapper response."""
    # MCP call_tool endpoint returns {"success": bool, "result": {...}, "response_time_ms": float}
    # The actual tool result is in result["result"]
    if "result" in result:
        return result["result"]
    return result


class TestInterAgentMessagingE2E:
    """E2E tests for inter-agent messaging through the daemon."""

    def test_parent_child_message_exchange(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Test complete parent<->child message exchange via MCP tools.

        This test verifies the full messaging flow:
        1. Parent sends message via send_message
        2. Child reads its inbox via get_inter_session_messages
        3. Child responds via send_message
        4. Parent reads the response via get_inter_session_messages
        """
        # Setup: Create parent and child sessions using register_session
        # (which creates entries in the sessions DB table)
        parent_external_id = f"parent-{uuid.uuid4().hex[:8]}"
        child_external_id = f"child-{uuid.uuid4().hex[:8]}"
        run_id = str(uuid.uuid4())

        # First, register the project in the database (required for FK constraint)
        project_result = cli_events.register_test_project(
            project_id="00000000-0000-0000-0000-000000000e2e",
            name="E2E Test Project",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in ["success", "already_exists"]

        # Register parent session first and get internal ID
        parent_result = cli_events.register_session(
            external_id=parent_external_id,
            machine_id="21000000-0000-4000-8000-000000000002",
            source="Claude Code",
            cwd=str(daemon_instance.project_dir),
        )
        parent_session_id = parent_result["id"]  # Internal session ID

        # Register child session with parent relationship
        child_result = cli_events.register_session(
            external_id=child_external_id,
            machine_id="21000000-0000-4000-8000-000000000002",
            source="Claude Code",
            parent_session_id=parent_session_id,
            cwd=str(daemon_instance.project_dir),
        )
        child_session_id = child_result["id"]  # Internal session ID

        # Register the child as a running agent in the agent registry
        register_result = cli_events.register_test_agent(
            run_id=run_id,
            session_id=child_session_id,
            parent_session_id=parent_session_id,
            mode="interactive",
        )
        assert register_result["status"] == "success"

        # Step 1: Parent sends message to child via send_message
        raw_result = mcp_client.call_tool(
            server_name="gobby-agents",
            tool_name="send_message",
            arguments={
                "from_session": parent_session_id,
                "target": "session",
                "target_id": child_session_id,
                "content": "Hello child, please process this task!",
            },
        )
        result = unwrap_result(raw_result)
        assert "error" not in result, f"send_message failed: {result}"
        assert "message" in result, f"No message in result: {result}"
        parent_to_child_msg_id = result["message"]["id"]
        assert parent_to_child_msg_id is not None

        # Step 2: Child reads its undelivered inbox.
        mcp_client.session_id = child_session_id
        mcp_client.agent_run_id = run_id
        raw_result = mcp_client.call_tool(
            server_name="gobby-agents",
            tool_name="get_inter_session_messages",
            arguments={
                "target_session_id": child_session_id,
                "direction": "inbox",
                "undelivered_only": True,
            },
        )
        result = unwrap_result(raw_result)
        assert "error" not in result, f"get_inter_session_messages failed: {result}"
        messages = result.get("messages", [])
        assert len(messages) >= 1, "Child should have received at least 1 message"

        # Find the message from parent by filtering on content and from_session
        received_msg = next(
            (
                m
                for m in messages
                if m["from_session"] == parent_session_id
                and m["content"] == "Hello child, please process this task!"
            ),
            None,
        )
        assert received_msg is not None, f"Expected message from parent not found in: {messages}"

        # Step 3: Child responds to parent via send_message
        raw_result = mcp_client.call_tool(
            server_name="gobby-agents",
            tool_name="send_message",
            arguments={
                "from_session": child_session_id,
                "target": "session",
                "target_id": parent_session_id,
                "content": "Task completed successfully, parent!",
            },
        )
        result = unwrap_result(raw_result)
        assert "error" not in result, f"send_message failed: {result}"
        assert "message" in result, f"No message in result: {result}"
        child_to_parent_msg_id = result["message"]["id"]
        assert child_to_parent_msg_id is not None

        # Step 4: Parent reads the response from its undelivered inbox.
        mcp_client.session_id = parent_session_id
        mcp_client.agent_run_id = None
        raw_result = mcp_client.call_tool(
            server_name="gobby-agents",
            tool_name="get_inter_session_messages",
            arguments={
                "target_session_id": parent_session_id,
                "direction": "inbox",
                "undelivered_only": True,
            },
        )
        result = unwrap_result(raw_result)
        assert "error" not in result, f"get_inter_session_messages (parent) failed: {result}"
        parent_messages = result.get("messages", [])
        assert len(parent_messages) >= 1, "Parent should have received at least 1 message"

        # Find the response message from child by filtering on content and from_session
        response_msg = next(
            (
                m
                for m in parent_messages
                if m["from_session"] == child_session_id
                and m["content"] == "Task completed successfully, parent!"
            ),
            None,
        )
        assert response_msg is not None, (
            f"Expected response from child not found in: {parent_messages}"
        )

        # Cleanup: Unregister the test agent
        cli_events.unregister_test_agent(run_id)

    def test_get_inter_session_messages_empty(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Reading an empty inbox returns an empty list."""
        # First, register the project in the database (required for FK constraint)
        project_result = cli_events.register_test_project(
            project_id="00000000-0000-0000-0000-000000000e2e",
            name="E2E Test Project",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in ["success", "already_exists"]

        # Register a session via register_session to get internal ID
        external_id = f"test-session-{uuid.uuid4().hex[:8]}"
        session_result = cli_events.register_session(
            external_id=external_id,
            machine_id="21000000-0000-4000-8000-000000000002",
            source="Claude Code",
            cwd=str(daemon_instance.project_dir),
        )
        session_id = session_result["id"]

        # The undelivered inbox should be empty.
        mcp_client.session_id = session_id
        raw_result = mcp_client.call_tool(
            server_name="gobby-agents",
            tool_name="get_inter_session_messages",
            arguments={
                "target_session_id": session_id,
                "direction": "inbox",
                "undelivered_only": True,
            },
        )
        result = unwrap_result(raw_result)

        assert "error" not in result, f"get_inter_session_messages failed: {result}"
        assert result.get("messages", []) == []
        assert result.get("count", 0) == 0

    def test_mailbox_reads_do_not_mark_messages_delivered(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Mailbox history reads are side-effect free."""
        parent_external_id = f"parent-{uuid.uuid4().hex[:8]}"
        child_external_id = f"child-{uuid.uuid4().hex[:8]}"
        run_id = str(uuid.uuid4())

        project_result = cli_events.register_test_project(
            project_id="00000000-0000-0000-0000-000000000e2e",
            name="E2E Test Project",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in ["success", "already_exists"]

        parent_result = cli_events.register_session(
            external_id=parent_external_id,
            machine_id="21000000-0000-4000-8000-000000000002",
            source="Claude Code",
            cwd=str(daemon_instance.project_dir),
        )
        parent_session_id = parent_result["id"]

        child_result = cli_events.register_session(
            external_id=child_external_id,
            machine_id="21000000-0000-4000-8000-000000000002",
            source="Claude Code",
            parent_session_id=parent_session_id,
            cwd=str(daemon_instance.project_dir),
        )
        child_session_id = child_result["id"]

        register_result = cli_events.register_test_agent(
            run_id=run_id,
            session_id=child_session_id,
            parent_session_id=parent_session_id,
            mode="interactive",
        )
        assert register_result["status"] == "success"

        # Send a message from parent to child
        mcp_client.call_tool(
            server_name="gobby-agents",
            tool_name="send_message",
            arguments={
                "from_session": parent_session_id,
                "target": "session",
                "target_id": child_session_id,
                "content": "Test delivery tracking",
            },
        )

        # First read returns the message.
        mcp_client.session_id = child_session_id
        mcp_client.agent_run_id = run_id
        raw_result = mcp_client.call_tool(
            server_name="gobby-agents",
            tool_name="get_inter_session_messages",
            arguments={
                "target_session_id": child_session_id,
                "direction": "inbox",
                "undelivered_only": True,
            },
        )
        result = unwrap_result(raw_result)
        first_messages = result.get("messages", [])
        assert len(first_messages) >= 1, "First read should return messages"
        first_message_id = first_messages[0]["id"]

        # A second read returns it again because history access does not acknowledge.
        raw_result = mcp_client.call_tool(
            server_name="gobby-agents",
            tool_name="get_inter_session_messages",
            arguments={
                "target_session_id": child_session_id,
                "direction": "inbox",
                "undelivered_only": True,
            },
        )
        result = unwrap_result(raw_result)
        second_messages = result.get("messages", [])
        assert len(second_messages) >= 1, "Second read should preserve messages"
        assert second_messages[0]["id"] == first_message_id

        # Cleanup
        cli_events.unregister_test_agent(run_id)

    def test_send_message_to_nonexistent_session(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Test send_message fails when target session doesn't exist."""
        project_result = cli_events.register_test_project(
            project_id="00000000-0000-0000-0000-000000000e2e",
            name="E2E Test Project",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in ["success", "already_exists"]

        external_id = f"test-session-{uuid.uuid4().hex[:8]}"
        session_result = cli_events.register_session(
            external_id=external_id,
            machine_id="21000000-0000-4000-8000-000000000002",
            source="Claude Code",
            cwd=str(daemon_instance.project_dir),
        )
        session_id = session_result["id"]

        # Try to send message to a non-existent session
        raw_result = mcp_client.call_tool(
            server_name="gobby-agents",
            tool_name="send_message",
            arguments={
                "from_session": session_id,
                "target": "session",
                "target_id": "00000000-0000-0000-0000-00000000dead",
                "content": "Hello!",
            },
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is False


class TestMessagingToolsAvailability:
    """Tests to verify messaging tools are properly registered."""

    def test_messaging_tools_are_registered(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Verify all messaging tools are available on gobby-agents server."""
        tools = mcp_client.list_tools(server_name="gobby-agents")
        tool_names = [t["name"] for t in tools]

        expected_tools = [
            "send_message",
            "get_inter_session_message",
            "get_inter_session_messages",
        ]

        for tool in expected_tools:
            assert tool in tool_names, f"Missing tool: {tool}"

    def test_send_message_schema(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Verify send_message tool schema can be retrieved."""
        raw_schema = mcp_client.get_tool_schema(
            server_name="gobby-agents",
            tool_name="send_message",
        )

        # Schema endpoint returns response - just verify we get some response
        assert raw_schema is not None
        assert isinstance(raw_schema, dict)

    def test_get_inter_session_message_schema(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Verify get_inter_session_message tool schema can be retrieved."""
        raw_schema = mcp_client.get_tool_schema(
            server_name="gobby-agents",
            tool_name="get_inter_session_message",
        )

        # Schema endpoint returns response - just verify we get some response
        assert raw_schema is not None
        assert isinstance(raw_schema, dict)
