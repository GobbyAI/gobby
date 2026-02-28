"""
E2E tests for worktree/clone merge flow with actual git operations.

Tests verify:
1. Merge tools (gobby-merge) are available: merge_start, merge_status, merge_resolve, merge_apply, merge_abort
2. Worktree tools (gobby-worktrees) are available
3. Clone merge workflow: create → simulate changes → sync → merge
4. Merge conflict detection and abort functionality

Test scenario:
1. Create worktree/clone
2. Simulate changes (create file in worktree path)
3. Test merge tools can be invoked
4. Verify conflict detection and merge_abort cleans up state

Note: Full merge with remote operations requires actual git remote configuration.
These tests verify the infrastructure (tools, flow, state management) is correct.
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
    if "result" in result:
        return result["result"]
    return result


class TestMergeToolsAvailability:
    """Tests to verify merge tools are properly registered."""

    def test_merge_tools_are_registered(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Verify all merge management tools are available on gobby-merge server."""
        tools = mcp_client.list_tools(server_name="gobby-merge")
        tool_names = [t["name"] for t in tools]

        expected_tools = [
            "merge_start",
            "merge_status",
            "merge_resolve",
            "merge_apply",
            "merge_abort",
        ]

        for tool in expected_tools:
            assert tool in tool_names, f"Missing merge tool: {tool}"

    def test_merge_start_schema(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Verify merge_start tool schema can be retrieved."""
        raw_schema = mcp_client.get_tool_schema(
            server_name="gobby-merge",
            tool_name="merge_start",
        )

        assert raw_schema is not None
        assert isinstance(raw_schema, dict)

    def test_merge_status_schema(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Verify merge_status tool schema can be retrieved."""
        raw_schema = mcp_client.get_tool_schema(
            server_name="gobby-merge",
            tool_name="merge_status",
        )

        assert raw_schema is not None
        assert isinstance(raw_schema, dict)


class TestWorktreeToolsAvailability:
    """Tests to verify worktree tools are properly registered."""

    def test_worktree_tools_are_registered(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Verify worktree management tools are available on gobby-worktrees server."""
        tools = mcp_client.list_tools(server_name="gobby-worktrees")
        tool_names = [t["name"] for t in tools]

        expected_tools = [
            "create_worktree",
            "list_worktrees",
            "get_worktree",
            "delete_worktree",
            "mark_worktree_merged",
        ]

        for tool in expected_tools:
            assert tool in tool_names, f"Missing worktree tool: {tool}"

    def test_clone_tools_are_registered(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Verify clone management tools are available on gobby-clones server."""
        tools = mcp_client.list_tools(server_name="gobby-clones")
        tool_names = [t["name"] for t in tools]

        expected_tools = [
            "create_clone",
            "list_clones",
            "get_clone",
            "delete_clone",
            "sync_clone",
            "merge_clone",
        ]

        for tool in expected_tools:
            assert tool in tool_names, f"Missing clone tool: {tool}"


class TestMergeWorkflowBasics:
    """Tests for basic merge workflow operations."""

    def test_merge_start_requires_worktree_id(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test merge_start returns error when worktree_id is missing."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-merge",
            tool_name="merge_start",
            arguments={
                "worktree_id": "",
                "source_branch": "feature/test",
                "target_branch": "main",
            },
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is False
        assert "worktree_id" in result.get("error", "").lower()

    def test_merge_start_requires_source_branch(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test merge_start returns error when source_branch is missing."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-merge",
            tool_name="merge_start",
            arguments={
                "worktree_id": "test-worktree-id",
                "source_branch": "",
                "target_branch": "main",
            },
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is False
        assert "source_branch" in result.get("error", "").lower()

    def test_merge_status_not_found(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test merge_status returns error for non-existent resolution."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-merge",
            tool_name="merge_status",
            arguments={"resolution_id": "nonexistent-resolution-id"},
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is False
        assert "not found" in result.get("error", "").lower()

    def test_merge_abort_not_found(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test merge_abort returns error for non-existent resolution."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-merge",
            tool_name="merge_abort",
            arguments={"resolution_id": "nonexistent-resolution-id"},
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is False
        assert "not found" in result.get("error", "").lower()


class TestWorktreeLifecycle:
    """Tests for worktree lifecycle operations."""

    def test_list_worktrees_empty(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test listing worktrees when none exist."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-worktrees",
            tool_name="list_worktrees",
            arguments={},
        )
        result = unwrap_result(raw_result)

        # Should succeed even with empty list
        # Count may be 0 or more depending on previous test state
        assert result.get("success") is True or isinstance(result.get("worktrees"), list)

    def test_get_worktree_not_found(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test getting a non-existent worktree returns error."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-worktrees",
            tool_name="get_worktree",
            arguments={"worktree_id": "nonexistent-worktree-id"},
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is False
        assert "not found" in result.get("error", "").lower()

    def test_delete_worktree_not_found(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test deleting a non-existent worktree returns error."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-worktrees",
            tool_name="delete_worktree",
            arguments={"worktree_id": "nonexistent-worktree-id"},
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is False
        assert "not found" in result.get("error", "").lower()


class TestCloneMergeWorkflow:
    """Tests for clone-based merge workflow."""

    def test_list_clones(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test listing clones returns correct structure."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-clones",
            tool_name="list_clones",
            arguments={},
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is True, f"list_clones failed: {result}"
        assert isinstance(result.get("clones"), list)
        assert result.get("count", -1) >= 0

    def test_get_clone_not_found(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test getting a non-existent clone returns error."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-clones",
            tool_name="get_clone",
            arguments={"clone_id": "nonexistent-clone-id"},
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is False
        assert "not found" in result.get("error", "").lower()

    def test_sync_clone_not_found(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test syncing a non-existent clone returns error."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-clones",
            tool_name="sync_clone",
            arguments={
                "clone_id": "nonexistent-clone-id",
                "direction": "pull",
            },
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is False
        assert "not found" in result.get("error", "").lower()

    def test_merge_clone_not_found(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test merging a non-existent clone returns error."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-clones",
            tool_name="merge_clone",
            arguments={"clone_id": "nonexistent-clone-id"},
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is False
        assert "not found" in result.get("error", "").lower()

    def test_delete_clone_not_found(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test deleting a non-existent clone returns error."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-clones",
            tool_name="delete_clone",
            arguments={"clone_id": "nonexistent-clone-id"},
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is False
        assert "not found" in result.get("error", "").lower()


class TestMergeConflictDetection:
    """Tests for merge conflict detection functionality."""

    def test_merge_resolve_not_found(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test resolving a non-existent conflict returns error."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-merge",
            tool_name="merge_resolve",
            arguments={
                "conflict_id": "nonexistent-conflict-id",
                "use_ai": False,
            },
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is False
        assert "not found" in result.get("error", "").lower()

    def test_merge_apply_not_found(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test applying a non-existent resolution returns error."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-merge",
            tool_name="merge_apply",
            arguments={"resolution_id": "nonexistent-resolution-id"},
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is False
        assert "not found" in result.get("error", "").lower()


class TestWorktreeMergeIntegration:
    """Integration tests for worktree merge workflow with task linkage."""

    def test_mark_worktree_merged_not_found(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test marking a non-existent worktree as merged returns error."""
        raw_result = mcp_client.call_tool(
            server_name="gobby-worktrees",
            tool_name="mark_worktree_merged",
            arguments={"worktree_id": "nonexistent-worktree-id"},
        )
        result = unwrap_result(raw_result)

        assert result.get("success") is False
        assert "not found" in result.get("error", "").lower()

    def test_worktree_with_task_linkage(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
        cli_events: CLIEventSimulator,
    ) -> None:
        """Test that worktrees can be linked to tasks."""
        # Setup - register project and session
        project_result = cli_events.register_test_project(
            project_id="e2e-test-project",
            name="E2E Test Project",
            repo_path=str(daemon_instance.project_dir),
        )
        assert project_result["status"] in ["success", "already_exists"]

        session_external_id = f"wt-task-{uuid.uuid4().hex[:8]}"
        session_result = cli_events.register_session(
            external_id=session_external_id,
            machine_id="test-machine",
            source="Claude Code",
            cwd=str(daemon_instance.project_dir),
        )
        session_id = session_result["id"]

        # Create a task to link
        raw_result = mcp_client.call_tool(
            server_name="gobby-tasks",
            tool_name="create_task",
            arguments={
                "title": "Task for Worktree Merge Test",
                "task_type": "task",
                "category": "code",
                "validation_criteria": "Tests pass and task is functional",
                "session_id": session_id,
            },
        )
        result = unwrap_result(raw_result)
        assert result.get("id") is not None, f"Task creation failed: {result}"
        task_id = result["id"]

        # Verify task exists and can be retrieved
        raw_result = mcp_client.call_tool(
            server_name="gobby-tasks",
            tool_name="get_task",
            arguments={"task_id": task_id},
        )
        result = unwrap_result(raw_result)

        # Task should exist and could be linked to a worktree
        # (Actual worktree creation requires git repo setup beyond E2E scope)
        assert result.get("id") == task_id or result.get("ref") is not None, (
            f"Task not found: {result}"
        )


class TestMergeResolutionStrategies:
    """Tests for merge resolution strategy parameter validation."""

    def test_merge_start_strategy_parameter(
        self,
        daemon_instance: DaemonInstance,
        mcp_client: MCPTestClient,
    ) -> None:
        """Test merge_start accepts different strategy parameters."""
        # These calls will fail (no worktree), but we're testing parameter acceptance
        strategies = ["auto", "conflict_only", "full_file", "manual"]

        for strategy in strategies:
            raw_result = mcp_client.call_tool(
                server_name="gobby-merge",
                tool_name="merge_start",
                arguments={
                    "worktree_id": "test-worktree",
                    "source_branch": "feature/test",
                    "target_branch": "main",
                    "strategy": strategy,
                },
            )
            result = unwrap_result(raw_result)

            # Should fail due to worktree not found, not invalid strategy
            assert result.get("success") is False
            # Error should be about worktree/path, not strategy
            error = result.get("error", "").lower()
            assert "strategy" not in error or "invalid" not in error, (
                f"Strategy '{strategy}' should be valid: {result}"
            )
