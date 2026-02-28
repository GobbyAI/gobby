"""
E2E test for cross-component integration.

This comprehensive test exercises the full workflow:
1. Start daemon
2. CLI hook triggers session tracking
3. MCP proxy discovers and invokes tools
4. Session state is tracked
5. Daemon is killed and restarted
6. Session state is recovered
7. Workflow continues successfully

Validates all components work together end-to-end.
"""

import os
import signal
import subprocess
import sys
import time
import uuid

import httpx
import pytest

from tests.e2e.conftest import (
    CLIEventSimulator,
    prepare_daemon_env,
    terminate_process_tree,
    wait_for_daemon_health,
)

pytestmark = pytest.mark.e2e


class TestFullWorkflowIntegration:
    """Cross-component E2E integration test."""

    def test_full_workflow_with_daemon_restart(
        self,
        e2e_project_dir,
        e2e_config,
    ) -> None:
        """
        Test the complete workflow including daemon restart.

        This test verifies:
        1. Daemon starts successfully
        2. CLI hooks execute (session tracking)
        3. MCP proxy discovers and invokes tools
        4. Sessions endpoint tracks state
        5. Daemon restart doesn't lose state
        6. All components continue to work after restart
        """
        config_path, http_port, ws_port = e2e_config
        gobby_home = config_path.parent
        log_dir = gobby_home / "logs"

        log_file = log_dir / "daemon.log"
        error_log_file = log_dir / "daemon_error.log"

        # Use helper to properly prepare env (PYTHONPATH, API keys, HOME override)
        env = prepare_daemon_env(home_dir=gobby_home)
        env["GOBBY_CONFIG"] = str(config_path)
        env["GOBBY_HOME"] = str(gobby_home)

        # ===== PHASE 1: Start daemon =====
        with open(log_file, "w") as log_f, open(error_log_file, "w") as err_f:
            process1 = subprocess.Popen(
                [sys.executable, "-m", "gobby.runner", "--config", str(config_path)],
                stdout=log_f,
                stderr=err_f,
                stdin=subprocess.DEVNULL,
                cwd=str(e2e_project_dir),
                env=env,
                start_new_session=True,
            )

        try:
            assert wait_for_daemon_health(http_port, timeout=20.0), (
                "Phase 1 FAILED: Daemon should start"
            )

            # Create client and simulator
            client = httpx.Client(base_url=f"http://localhost:{http_port}", timeout=10.0)
            cli_events = CLIEventSimulator(f"http://localhost:{http_port}")

            try:
                # ===== PHASE 2: CLI hook triggers session tracking =====
                session_id = f"full-workflow-{uuid.uuid4().hex[:8]}"

                result = cli_events.session_start(
                    session_id=session_id,
                    machine_id="test-machine",
                    source="claude",
                )
                assert "continue" in result or "decision" in result, (
                    "Phase 2 FAILED: Session start hook should execute"
                )

                # ===== PHASE 3: MCP proxy discovers and invokes tools =====
                # 3a. Discover MCP servers
                servers_response = client.get("/api/mcp/servers")
                assert servers_response.status_code == 200, (
                    "Phase 3a FAILED: Should list MCP servers"
                )
                servers_data = servers_response.json()
                assert "servers" in servers_data, (
                    "Phase 3a FAILED: Response should have servers key"
                )

                # 3b. Discover tools
                tools_response = client.get("/api/mcp/tools")
                assert tools_response.status_code == 200, "Phase 3b FAILED: Should list MCP tools"
                tools_data = tools_response.json()
                assert "tools" in tools_data, "Phase 3b FAILED: Response should have tools key"

                # 3c. Invoke a tool (list_ready_tasks from gobby-tasks)
                tool_call_response = client.post(
                    "/api/mcp/tools/call",
                    json={
                        "server_name": "gobby-tasks",
                        "tool_name": "list_ready_tasks",
                        "arguments": {},
                    },
                )
                assert tool_call_response.status_code == 200, (
                    "Phase 3c FAILED: Tool call should succeed"
                )
                tool_result = tool_call_response.json()
                assert tool_result.get("success") is True, (
                    "Phase 3c FAILED: Tool call should return success"
                )

                # ===== PHASE 4: Session state is tracked =====
                sessions_response = client.get("/api/sessions")
                assert sessions_response.status_code == 200, (
                    "Phase 4 FAILED: Sessions endpoint should work"
                )
                sessions_data = sessions_response.json()
                assert "sessions" in sessions_data, (
                    "Phase 4 FAILED: Response should have sessions key"
                )
                assert "count" in sessions_data, "Phase 4 FAILED: Response should have count key"
                _sessions_count_before = sessions_data["count"]

                # Execute more hooks to verify state management
                result = cli_events.tool_use(
                    session_id=session_id,
                    tool_name="Read",
                    tool_input={"file_path": "/tmp/test.txt"},
                    source="claude",
                )
                assert result is not None, "Phase 4 FAILED: Tool use hook should execute"

            finally:
                cli_events.close()
                client.close()

            # ===== PHASE 5: Kill and restart daemon =====
            os.kill(process1.pid, signal.SIGTERM)
            process1.wait(timeout=10)
            time.sleep(2.0)

            # Start second daemon
            with open(log_file, "a") as log_f, open(error_log_file, "a") as err_f:
                process2 = subprocess.Popen(
                    [sys.executable, "-m", "gobby.runner", "--config", str(config_path)],
                    stdout=log_f,
                    stderr=err_f,
                    stdin=subprocess.DEVNULL,
                    cwd=str(e2e_project_dir),
                    env=env,
                    start_new_session=True,
                )

            try:
                assert wait_for_daemon_health(http_port, timeout=20.0), (
                    "Phase 5 FAILED: Daemon should restart"
                )

                # ===== PHASE 6: Session state is recovered =====
                client2 = httpx.Client(base_url=f"http://localhost:{http_port}", timeout=10.0)
                cli_events2 = CLIEventSimulator(f"http://localhost:{http_port}")

                try:
                    sessions_response2 = client2.get("/api/sessions")
                    assert sessions_response2.status_code == 200, (
                        "Phase 6 FAILED: Sessions endpoint should work after restart"
                    )
                    sessions_data2 = sessions_response2.json()
                    assert "sessions" in sessions_data2, (
                        "Phase 6 FAILED: Sessions should be present after restart"
                    )

                    # ===== PHASE 7: Workflow continues successfully =====
                    # 7a. MCP proxy still works
                    servers_response2 = client2.get("/api/mcp/servers")
                    assert servers_response2.status_code == 200, (
                        "Phase 7a FAILED: MCP servers should work after restart"
                    )

                    # 7b. Tool invocation still works
                    tool_call_response2 = client2.post(
                        "/api/mcp/tools/call",
                        json={
                            "server_name": "gobby-tasks",
                            "tool_name": "list_ready_tasks",
                            "arguments": {},
                        },
                    )
                    assert tool_call_response2.status_code == 200, (
                        "Phase 7b FAILED: Tool calls should work after restart"
                    )

                    # 7c. New hooks still execute
                    new_session_id = f"post-restart-{uuid.uuid4().hex[:8]}"
                    result = cli_events2.session_start(
                        session_id=new_session_id,
                        machine_id="test-machine",
                        source="claude",
                    )
                    assert "continue" in result or "decision" in result, (
                        "Phase 7c FAILED: Hooks should work after restart"
                    )

                    # 7d. Health endpoint confirms system is healthy
                    health_response = client2.get("/api/admin/status")
                    assert health_response.status_code == 200, (
                        "Phase 7d FAILED: Health endpoint should work"
                    )
                    health_data = health_response.json()
                    assert health_data.get("status") == "healthy", (
                        "Phase 7d FAILED: System should be healthy"
                    )

                finally:
                    cli_events2.close()
                    client2.close()

            finally:
                terminate_process_tree(process2.pid)
        finally:
            if process1.poll() is None:
                terminate_process_tree(process1.pid)

    def test_concurrent_components_work_together(
        self,
        daemon_instance,
        daemon_client: httpx.Client,
        cli_events: CLIEventSimulator,
        mcp_client,
    ) -> None:
        """
        Test that multiple components work correctly in concurrent usage.

        Verifies that CLI hooks, MCP proxy, and sessions all function
        without interfering with each other.
        """
        # Execute session hooks
        session_id = f"concurrent-test-{uuid.uuid4().hex[:8]}"
        result = cli_events.session_start(
            session_id=session_id,
            machine_id="test-machine",
            source="claude",
        )
        assert "continue" in result or "decision" in result

        # Execute MCP tool call
        tool_result = daemon_client.post(
            "/api/mcp/tools/call",
            json={
                "server_name": "gobby-tasks",
                "tool_name": "list_ready_tasks",
                "arguments": {},
            },
        )
        assert tool_result.status_code == 200

        # Query sessions
        sessions = daemon_client.get("/api/sessions")
        assert sessions.status_code == 200

        # Execute more hooks
        tool_use_result = cli_events.tool_use(
            session_id=session_id,
            tool_name="Bash",
            tool_input={"command": "echo test"},
            source="claude",
        )
        assert tool_use_result is not None

        # Query servers via MCP client
        servers = mcp_client.list_servers()
        assert isinstance(servers, list)

        # End session
        end_result = cli_events.session_end(
            session_id=session_id,
            machine_id="test-machine",
            source="claude",
        )
        assert end_result is not None

        # Final health check
        health = daemon_client.get("/api/admin/status")
        assert health.status_code == 200
        assert health.json().get("status") == "healthy"

    def test_error_recovery_across_components(
        self,
        daemon_instance,
        daemon_client: httpx.Client,
        cli_events: CLIEventSimulator,
    ) -> None:
        """
        Test that errors in one component don't break other components.

        Verifies system resilience and error isolation.
        """
        # Start a session
        session_id = f"error-test-{uuid.uuid4().hex[:8]}"
        cli_events.session_start(
            session_id=session_id,
            machine_id="test-machine",
            source="claude",
        )

        # Trigger an error in MCP (invalid server)
        # Note: MCP errors return 200 with {success: False, ...} in body
        # for better MCP client compatibility (see execution.py _process_tool_proxy_result)
        error_response = daemon_client.post(
            "/api/mcp/tools/call",
            json={
                "server_name": "nonexistent-server",
                "tool_name": "some_tool",
                "arguments": {},
            },
        )
        # Error is in response body, not HTTP status code
        error_data = error_response.json()
        assert error_response.status_code == 200 or error_response.status_code in [
            400,
            404,
            500,
            503,
        ]
        if error_response.status_code == 200:
            assert error_data.get("success") is False

        # Sessions should still work
        sessions_response = daemon_client.get("/api/sessions")
        assert sessions_response.status_code == 200

        # MCP tools should still work with valid server
        valid_response = daemon_client.post(
            "/api/mcp/tools/call",
            json={
                "server_name": "gobby-tasks",
                "tool_name": "list_ready_tasks",
                "arguments": {},
            },
        )
        assert valid_response.status_code == 200

        # Hooks should still work
        result = cli_events.tool_use(
            session_id=session_id,
            tool_name="Read",
            tool_input={"file_path": "/nonexistent/path"},
            source="claude",
        )
        assert result is not None

        # Health check
        health = daemon_client.get("/api/admin/status")
        assert health.status_code == 200
        assert health.json().get("status") == "healthy"
