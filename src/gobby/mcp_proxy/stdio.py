"""
Stdio MCP wrapper for Gobby daemon.

Provides a stdio-based MCP server that:
1. Proxies to the daemon's HTTP MCP server
2. Adds lifecycle management tools (start/stop/restart)
3. Auto-starts daemon if not running
4. Suitable for Claude Code integration
"""

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import httpx
from fastmcp import FastMCP
from gobby.config.app import load_config
from gobby.mcp_proxy.tools.internal import InternalRegistryManager
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.database import LocalDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager

logger = logging.getLogger(__name__)


def get_daemon_pid() -> int | None:
    """
    Get daemon PID from PID file.

    Returns:
        PID if daemon is running, None otherwise
    """
    pid_file = Path.home() / ".gobby" / "gobby.pid"
    if not pid_file.exists():
        return None

    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        # Check if process exists
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return None


def is_daemon_running() -> bool:
    """Check if daemon is running."""
    return get_daemon_pid() is not None


async def start_daemon_process() -> dict[str, Any]:
    """
    Start the Gobby daemon process.

    Returns:
        Result with success status and message
    """
    if is_daemon_running():
        return {
            "success": False,
            "already_running": True,
            "message": "Daemon is already running",
            "pid": get_daemon_pid(),
        }

    try:
        # Start daemon using subprocess
        # Use sys.executable to ensure we run in the same environment and don't rely on PATH
        result = subprocess.run(
            [sys.executable, "-m", "gobby.cli", "start"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            # Give daemon time to start
            await asyncio.sleep(2)
            pid = get_daemon_pid()
            return {
                "success": True,
                "message": "Daemon started successfully",
                "pid": pid,
                "output": result.stdout,
            }
        else:
            return {
                "success": False,
                "message": "Failed to start daemon",
                "error": result.stderr,
                "returncode": result.returncode,
            }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "message": "Daemon start command timed out",
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Failed to start daemon: {e}",
            "error": str(e),
        }


async def stop_daemon_process() -> dict[str, Any]:
    """
    Stop the Gobby daemon process.

    Returns:
        Result with success status and message
    """
    pid = get_daemon_pid()
    if not pid:
        return {
            "success": False,
            "not_running": True,
            "message": "Daemon is not running",
        }

    try:
        result = subprocess.run(
            ["gobby", "stop"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            return {
                "success": True,
                "message": "Daemon stopped successfully",
                "output": result.stdout,
            }
        else:
            return {
                "success": False,
                "message": "Failed to stop daemon",
                "error": result.stderr,
                "returncode": result.returncode,
            }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "message": "Daemon stop command timed out",
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Failed to stop daemon: {e}",
            "error": str(e),
        }


async def restart_daemon_process() -> dict[str, Any]:
    """
    Restart the Gobby daemon process.

    Returns:
        Result with success status and message
    """
    try:
        result = subprocess.run(
            ["gobby", "restart"],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode == 0:
            # Give daemon time to restart
            await asyncio.sleep(2)
            pid = get_daemon_pid()
            return {
                "success": True,
                "message": "Daemon restarted successfully",
                "pid": pid,
                "output": result.stdout,
            }
        else:
            return {
                "success": False,
                "message": "Failed to restart daemon",
                "error": result.stderr,
                "returncode": result.returncode,
            }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "message": "Daemon restart command timed out",
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Failed to restart daemon: {e}",
            "error": str(e),
        }


async def check_daemon_http_health(port: int, timeout: float = 2.0) -> bool:
    """
    Check if daemon HTTP server is responding.

    Args:
        port: Daemon HTTP port
        timeout: Request timeout in seconds

    Returns:
        True if daemon is healthy
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://localhost:{port}/admin/status",
                timeout=timeout,
            )
            return response.status_code == 200
    except Exception:
        return False


async def _call_daemon_tool(
    daemon_port: int,
    tool_name: str,
    arguments: dict[str, Any],
    timeout: float = 10.0,
) -> dict[str, Any]:
    """
    Generic helper to proxy tool calls to the HTTP daemon's MCP server.

    Args:
        daemon_port: Daemon HTTP port
        tool_name: Name of the tool to call on the daemon
        arguments: Arguments to pass to the tool
        timeout: Request timeout in seconds

    Returns:
        Tool execution result from daemon
    """
    if not is_daemon_running():
        return {
            "success": False,
            "error": "Daemon is not running. Start it with start() first.",
        }

    try:
        async with httpx.AsyncClient() as client:
            # Note: We use /mcp/ as the endpoint because FastMCP is mounted there
            # and we are using Streamable HTTP transport (stateless)
            response = await client.post(
                f"http://localhost:{daemon_port}/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": arguments,
                    },
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                },
                timeout=timeout,
            )

            if response.status_code == 200:
                # Try parsing as standard JSON first (new behavior with json_response=True)
                try:
                    result = response.json()
                    if "result" in result and "structuredContent" in result["result"]:
                        return cast(dict[str, Any], result["result"]["structuredContent"])
                    elif "result" in result:
                        return cast(dict[str, Any], result["result"])
                    elif "error" in result:
                        return {
                            "success": False,
                            "error": result["error"].get("message", str(result["error"])),
                        }
                except ValueError:
                    # Fallback to SSE parsing (old behavior)
                    text = response.text
                    if "event: message" in text and "data: " in text:
                        import json

                        try:
                            json_str = text.split("data: ", 1)[1].strip()
                            result = json.loads(json_str)
                            if "result" in result and "structuredContent" in result["result"]:
                                return cast(dict[str, Any], result["result"]["structuredContent"])
                            elif "result" in result:
                                return cast(dict[str, Any], result["result"])
                            elif "error" in result:
                                return {
                                    "success": False,
                                    "error": result["error"].get("message", str(result["error"])),
                                }
                        except Exception:
                            pass

                return {
                    "success": False,
                    "error": f"Unexpected response format: {response.text[:200]}",
                }
            else:
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}: {response.text}",
                }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def create_stdio_mcp_server() -> FastMCP:
    """
    Create stdio MCP server with daemon lifecycle management.

    This creates a hybrid MCP server that:
    1. Proxies all tools/resources from the HTTP daemon's MCP server
    2. Adds additional lifecycle management tools (start/stop/restart)

    Returns:
        Configured FastMCP server instance
    """
    # Load config to get daemon port, websocket port, and timeout settings
    config = load_config()
    daemon_port = config.daemon_port
    websocket_port = config.websocket.port

    # Extract timeout settings from config
    default_tool_timeout = 10.0  # Default for most tools
    long_operation_timeout = 30.0  # For operations like sync, add/remove servers

    # Create base MCP server with lifecycle tools
    # We'll add the HTTP proxy tools below if daemon is running
    mcp = FastMCP(name="Gobby Daemon (Stdio)")

    # Initialize internal tool registries locally for stdio access
    # This allows direct DB access for tasks without proxying through HTTP
    internal_manager = InternalRegistryManager()

    try:
        db = LocalDatabase()
        task_manager = LocalTaskManager(db)
        sync_manager = TaskSyncManager(task_manager)

        # Wire up change listener for automatic export in this process too
        task_manager.add_change_listener(sync_manager.trigger_export)

        # Create task registry for internal tools
        internal_manager.add_registry(create_task_registry(task_manager, sync_manager))
        logger.debug("✅ Created internal task registry in stdio server")
    except Exception as e:
        logger.error(f"Failed to create task registry: {e}")

    # ===== DAEMON LIFECYCLE TOOLS =====

    @mcp.tool
    async def start() -> dict[str, Any]:
        """
        Start the Gobby daemon.

        Use this when the daemon is not running and you need to start it.
        The daemon provides access to Claude Code sessions, MCP servers, and Gobby platform features.

        Example usage:
        - When status() shows the daemon is not running
        - Before attempting to use list_sessions() or call_tool()
        - After a system restart

        Returns:
            Result with success status, PID, health check status, and formatted status message
        """
        result = await start_daemon_process()

        # Wait for daemon to be healthy if start was successful
        if result.get("success"):
            # Wait up to 10 seconds for daemon to be healthy
            for _ in range(10):
                if await check_daemon_http_health(daemon_port):
                    result["healthy"] = True
                    break
                await asyncio.sleep(1)
            else:
                result["healthy"] = False
                result["warning"] = "Daemon started but health check failed"

            # Get formatted status after successful start
            if result["healthy"]:
                status_result = await _get_status()
                result["formatted_message"] = status_result.get("formatted_message")

        return result

    @mcp.tool
    async def stop() -> dict[str, Any]:
        """
        Stop the Gobby daemon.

        Use this to gracefully shut down the daemon process.
        WARNING: After stopping, MCP tools that require the daemon will not work until you call start().

        Example usage:
        - When you're done working and want to shut down cleanly
        - Before system maintenance or updates
        - To reset the daemon state (stop then start)

        Returns:
            Result with success status and message
        """
        return await stop_daemon_process()

    @mcp.tool
    async def restart() -> dict[str, Any]:
        """
        Restart the Gobby daemon.

        Use this to apply configuration changes or recover from errors.
        The daemon will be stopped and then started with a fresh process.

        Example usage:
        - After updating daemon configuration
        - When the daemon is unhealthy (status shows healthy=false)
        - To clear stuck sessions or connections
        - After updating MCP server configurations

        Returns:
            Result with success status, new PID, and health check status
        """
        result = await restart_daemon_process()

        # Wait for daemon to be healthy if restart was successful
        if result.get("success"):
            for _ in range(10):
                if await check_daemon_http_health(daemon_port):
                    result["healthy"] = True
                    break
                await asyncio.sleep(1)
            else:
                result["healthy"] = False
                result["warning"] = "Daemon restarted but health check failed"

        return result

    async def _get_status() -> dict[str, Any]:
        """Internal helper to get status."""
        from pathlib import Path

        from gobby.utils.status import format_status_message

        pid = get_daemon_pid()
        is_running = pid is not None
        is_healthy = False

        result = {
            "running": is_running,
            "pid": pid,
            "healthy": is_healthy,
            "http_port": daemon_port,
            "websocket_port": websocket_port,
        }

        # Get detailed status from daemon if it's running and healthy
        daemon_status = None
        if is_running:
            is_healthy = await check_daemon_http_health(daemon_port)
            result["healthy"] = is_healthy

            if is_healthy:
                try:
                    async with httpx.AsyncClient() as client:
                        response = await client.get(
                            f"http://localhost:{daemon_port}/admin/status",
                            timeout=2.0,
                        )
                        if response.status_code == 200:
                            daemon_status = response.json()
                            result["daemon_details"] = daemon_status
                except Exception as e:
                    result["status_error"] = str(e)

        # Format status message
        if is_running and daemon_status:
            pid_file = str(Path.home() / ".gobby" / "gobby.pid")
            log_files = str(Path.home() / ".gobby" / "logs")

            formatted_message = format_status_message(
                running=True,
                pid=pid,
                pid_file=pid_file,
                log_files=log_files,
                http_port=daemon_port,
                websocket_port=websocket_port,
            )
        else:
            formatted_message = format_status_message(running=False)

        result["formatted_message"] = formatted_message
        return result

    @mcp.tool
    async def status() -> dict[str, Any]:
        """
        Get comprehensive daemon status and health information.

        Use this to check if the daemon is running and healthy before performing operations.
        Always call this first when troubleshooting issues.

        Returns a dictionary with:
        - running: Whether the daemon process is running (bool)
        - pid: Process ID if running (int or null)
        - healthy: Whether the daemon is responding to HTTP requests (bool)
        - http_port: Daemon's HTTP port (typically 8765)
        - websocket_port: Daemon's WebSocket port (typically 8766)
        - daemon_details: Additional status info if daemon is healthy
        - formatted_message: Human-readable status display

        Example workflow:
        1. Call status() to check current state
        2. If not running, call start()
        3. If running but not healthy, call restart()

        Returns:
            Daemon status dictionary with running, pid, healthy, and port information
        """
        return await _get_status()

    @mcp.tool
    async def init_project(
        name: str | None = None, github_url: str | None = None
    ) -> dict[str, Any]:
        """
        Initialize a new Gobby project in the current directory.

        This tool:
        1. Creates a new project in local storage
        2. Generates a local .gobby/project.json file
        3. Sets up project-specific MCP configuration

        Args:
            name: Optional project name (auto-detected from directory name if not provided)
            github_url: Optional GitHub URL (auto-detected from git remote if not provided)

        Returns:
            Dict with success status and project details
        """
        from pathlib import Path

        from gobby.utils.project_init import initialize_project

        try:
            cwd = Path.cwd()
            result = initialize_project(cwd=cwd, name=name, github_url=github_url)

            project_json_path = cwd / ".gobby" / "project.json"

            if result.already_existed:
                return {
                    "success": True,
                    "message": f"Project '{result.project_name}' already initialized",
                    "project": {
                        "id": result.project_id,
                        "name": result.project_name,
                        "created_at": result.created_at,
                    },
                    "paths": {
                        "project_json": str(project_json_path),
                    },
                }

            return {
                "success": True,
                "message": f"Project '{result.project_name}' initialized successfully",
                "project": {
                    "id": result.project_id,
                    "name": result.project_name,
                    "created_at": result.created_at,
                },
                "paths": {
                    "project_json": str(project_json_path),
                },
            }

        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool
    async def call_tool(
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Execute a tool on a connected MCP server.

        This is the primary way to interact with MCP servers (Supabase, memory, etc.)
        through the Gobby daemon.

        Args:
            server_name: Name of the MCP server
                Examples: "supabase", "gobby-memory", "context7"
                Internal servers: "gobby-tasks", "gobby-hooks"
            tool_name: Name of the specific tool to execute
                Example: "list_tables", "search_memory_nodes", "get-library-docs"
            arguments: Dictionary of arguments required by the tool (optional)
                Example: {"schema": "public"} or {"query": "react hooks"}

        Example usage:
        1. List Supabase tables:
           call_tool("supabase", "list_tables", {"schemas": ["public"]})

        2. Search memory:
           call_tool("gobby-memory", "search_memory_nodes", {"query": "authentication"})

        3. Get library docs:
           call_tool("context7", "get-library-docs", {"libraryId": "/react/react"})

        4. Create a task (internal):
           call_tool("gobby-tasks", "create_task", {"title": "My task"})

        Workflow:
        1. Use list_tools(server_name) to see available tools
        2. Review tool parameters and requirements
        3. Call call_tool() with appropriate arguments

        Requires:
        - Daemon must be running (for external servers)
        - MCP server must be configured and enabled
        - Tool must exist on the specified server

        Returns:
            Dictionary with success status and tool execution result
        """
        # Route internal tools locally (no HTTP round-trip)
        if internal_manager.is_internal(server_name):
            registry = internal_manager.get_registry(server_name)
            if not registry:
                available = ", ".join(r.name for r in internal_manager.get_all_registries())
                return {
                    "success": False,
                    "server": server_name,
                    "error": f"Internal server '{server_name}' not found",
                    "available_internal_servers": available,
                }
            try:
                result = await registry.call(tool_name, arguments or {})
                return {"success": True, "result": result}
            except ValueError as e:
                return {
                    "success": False,
                    "server": server_name,
                    "tool": tool_name,
                    "error": str(e),
                    "error_type": "ValueError",
                }
            except Exception as e:
                logger.error(f"Failed to call internal tool {tool_name}: {e}")
                return {
                    "success": False,
                    "server": server_name,
                    "tool": tool_name,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }

        # Route external tools through HTTP daemon
        return await _call_daemon_tool(
            daemon_port=daemon_port,
            tool_name="call_tool",
            arguments={
                "server_name": server_name,
                "tool_name": tool_name,
                "arguments": arguments or {},
            },
            timeout=long_operation_timeout,
        )

    @mcp.tool
    async def add_mcp_server(
        name: str,
        transport: str,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """
        Add a new MCP server to the daemon's configuration.

        Dynamically adds and connects to a new MCP server without restarting the daemon.
        Supports multiple transport types: http, stdio, websocket.

        Args:
            name: Unique server name (e.g., "supabase", "context7")
            transport: Transport type - "http", "stdio", or "websocket"
            url: Server URL (required for http/websocket, e.g., "http://localhost:6543/mcp")
            headers: Custom HTTP headers (optional, e.g., {"CONTEXT7_API_KEY": "ctx7sk-..."})
            command: Command to run (required for stdio, e.g., "uv", "npx")
            args: Command arguments (optional for stdio, e.g., ["run", "server.py"])
            env: Environment variables (optional for stdio, e.g., {"DEBUG": "true"})
            enabled: Whether server is enabled (default: True)

        Returns:
            Result dict with success status, connection state, and any errors

        Example HTTP server:
            add_mcp_server(
                name="supabase",
                transport="http",
                url="http://localhost:6543/mcp"
            )

        Example HTTP with API key:
            add_mcp_server(
                name="context7",
                transport="http",
                url="https://mcp.context7.com/mcp",
                headers={"CONTEXT7_API_KEY": "ctx7sk-..."}
            )

        Example stdio server:
            add_mcp_server(
                name="weather",
                transport="stdio",
                command="uv",
                args=["run", "weather_server.py"],
                env={"API_KEY": "secret"}
            )
        """
        arguments = {"name": name, "transport": transport, "enabled": enabled}
        if url:
            arguments["url"] = url
        if headers:
            arguments["headers"] = headers
        if command:
            arguments["command"] = command
        if args:
            arguments["args"] = args
        if env:
            arguments["env"] = env

        return await _call_daemon_tool(
            daemon_port=daemon_port,
            tool_name="add_mcp_server",
            arguments=arguments,
            timeout=long_operation_timeout,
        )

    @mcp.tool
    async def remove_mcp_server(name: str) -> dict[str, Any]:
        """
        Remove an MCP server from the daemon's configuration.

        Disconnects and removes the server without restarting the daemon.

        Args:
            name: Server name to remove (e.g., "supabase", "context7")

        Returns:
            Result dict with success status

        Example:
            remove_mcp_server(name="supabase")
        """
        return await _call_daemon_tool(
            daemon_port=daemon_port,
            tool_name="remove_mcp_server",
            arguments={"name": name},
            timeout=default_tool_timeout,
        )

    @mcp.tool
    async def import_mcp_server(
        from_project: str | None = None,
        servers: list[str] | None = None,
        github_url: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """
        Import MCP servers from various sources.

        Three import modes:
        1. **From project**: Copy servers from another Gobby project
        2. **From GitHub**: Parse repository README to extract config
        3. **From query**: Search web to find and configure an MCP server

        If no secrets are needed, the server is added immediately.
        If secrets are needed (API keys), returns a config to fill in and pass to add_mcp_server().

        Args:
            from_project: Source project name to import servers from
            servers: Optional list of specific server names to import (imports all if None)
            github_url: GitHub repository URL to parse for MCP server config
            query: Natural language search query (e.g., "exa search mcp server")

        Returns:
            On success: {"success": True, "imported": ["server1", "server2"]}
            Needs secrets: {"status": "needs_configuration", "config": {...}, "missing": ["API_KEY"]}
                          (pass the filled config to add_mcp_server())

        Examples:
            # Import all servers from another project
            import_mcp_server(from_project="my-other-project")

            # Import specific servers
            import_mcp_server(from_project="gobby", servers=["supabase", "context7"])

            # Import from GitHub
            import_mcp_server(github_url="https://github.com/anthropics/mcp-filesystem")

            # Search and import
            import_mcp_server(query="exa search mcp server")
        """
        arguments: dict[str, Any] = {}
        if from_project is not None:
            arguments["from_project"] = from_project
        if servers is not None:
            arguments["servers"] = servers
        if github_url is not None:
            arguments["github_url"] = github_url
        if query is not None:
            arguments["query"] = query

        return await _call_daemon_tool(
            daemon_port=daemon_port,
            tool_name="import_mcp_server",
            arguments=arguments,
            timeout=120,  # Longer timeout for web search/fetch
        )

    @mcp.tool
    async def list_mcp_servers() -> dict[str, Any]:
        """
        List all MCP servers configured in the daemon.

        Returns details about each MCP server including connection status,
        available tools, and resources.

        Returns:
            Dict with servers list, total count, and connected count:
            {
                "servers": [
                    {
                        "name": "context7",
                        "state": "connected",
                        "connected": true,
                        "transport": "http",
                        "tools": [...],
                        "tool_count": 5
                    }
                ],
                "total_count": 3,
                "connected_count": 2
            }

        Example:
            result = list_mcp_servers()
            for server in result["servers"]:
                print(f"{server['name']}: {server['state']}")
        """
        return await _call_daemon_tool(
            daemon_port=daemon_port,
            tool_name="list_mcp_servers",
            arguments={},
            timeout=default_tool_timeout,
        )

    @mcp.tool
    async def recommend_tools(task_description: str, agent_id: str | None = None) -> dict[str, Any]:
        """
        Get intelligent tool recommendations for a given task.

        Uses Claude Sonnet 4.5 to analyze your task and recommend which MCP tools
        from your connected servers would be most helpful. Returns recommendations
        with suggested tool names, arguments, and workflow steps.

        Args:
            task_description: Description of what you're trying to accomplish
                             (e.g., "Find React hooks documentation", "List database tables")
            agent_id: Optional agent profile ID to filter tools by assigned permissions
                     (e.g., "frontend-dev" agent only sees frontend-related tools)

        Returns:
            Dict with tool recommendations and usage suggestions:
            {
                "success": True,
                "task": "Find React hooks documentation",
                "recommendation": "I recommend using context7 tools...",
                "agent_profile": "frontend-dev" (if filtered),
                "available_servers": ["context7", "playwright"],
                "total_tools": 25
            }

        Example:
            recommend_tools("Find documentation for Supabase auth")
            recommend_tools("Debug frontend issue", agent_id="frontend-dev")
        """
        arguments = {"task_description": task_description}
        if agent_id:
            arguments["agent_id"] = agent_id

        return await _call_daemon_tool(
            daemon_port=daemon_port,
            tool_name="recommend_tools",
            arguments=arguments,
            timeout=long_operation_timeout,
        )

    @mcp.tool
    async def list_tools(server: str | None = None) -> dict[str, Any]:
        """
        List tools from MCP servers.

        Use this to discover tools available on servers.

        Args:
            server: Optional server name (e.g., "context7", "supabase").
                   If not provided, returns tools from all servers.
                   Internal servers: "gobby-tasks", "gobby-hooks", etc.

        Returns:
            Dict with tool listings:
            - If server specified: {"server": "context7", "tools": [{name, brief}, ...]}
            - If no server: {"servers": [{name, tools: [{name, brief}]}, ...]}

        Example:
            # List internal task tools
            list_tools(server="gobby-tasks")
            > {"server": "gobby-tasks", "tools": [
                {"name": "create_task", "brief": "Create a new task in the current project."},
                {"name": "list_tasks", "brief": "List tasks with optional filters."}
              ]}

            # List all tools across all servers
            list_tools()
            > {"servers": [
                {"name": "gobby-tasks", "tools": [...]},
                {"name": "context7", "tools": [...]},
                {"name": "supabase", "tools": [...]}
              ]}
        """
        # Handle internal servers locally
        if server and internal_manager.is_internal(server):
            registry = internal_manager.get_registry(server)
            if not registry:
                available = ", ".join(r.name for r in internal_manager.get_all_registries())
                return {
                    "success": False,
                    "error": f"Internal server '{server}' not found",
                    "available_internal_servers": available,
                }
            return {"success": True, "tools": registry.list_tools()}

        # For external servers or listing all, proxy to daemon
        # The daemon will include internal servers in its response
        arguments = {"server": server} if server else {}
        return await _call_daemon_tool(
            daemon_port=daemon_port,
            tool_name="list_tools",
            arguments=arguments,
            timeout=default_tool_timeout,
        )

    @mcp.tool
    async def read_mcp_resource(server_name: str, resource_uri: str) -> dict[str, Any]:
        """
        Read a resource from a downstream MCP server.

        Args:
            server_name: Name of the MCP server
            resource_uri: URI of the resource to read

        Returns:
            Resource contents

        Raises:
            ValueError: If server not found or not connected
            Exception: If resource read fails
        """
        return await _call_daemon_tool(
            daemon_port=daemon_port,
            tool_name="read_mcp_resource",
            arguments={
                "server_name": server_name,
                "resource_uri": resource_uri,
            },
            timeout=30.0,
        )

    @mcp.tool
    async def get_tool_schema(server_name: str, tool_name: str) -> dict[str, Any]:
        """
        Get full schema (inputSchema) for a specific MCP tool.

        Reads the complete tool definition including the detailed inputSchema.
        This provides fast, offline access to tool schemas.

        Use list_tools() first to discover available tools, then use this to get
        full details before calling the tool.

        Args:
            server_name: Name of the MCP server (e.g., "context7", "supabase")
                Internal servers: "gobby-tasks", "gobby-hooks"
            tool_name: Name of the tool (e.g., "get-library-docs", "list_tables")

        Returns:
            Dict with tool name, description, and full inputSchema:
            {
                "success": True,
                "server": "context7",
                "tool": {
                    "name": "get-library-docs",
                    "description": "Fetches comprehensive documentation...",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "libraryId": {"type": "string", ...}
                        },
                        "required": ["libraryId"]
                    }
                }
            }

        Example:
            # First discover tools
            list_tools(server="context7")

            # Then get full schema
            get_tool_schema(server_name="context7", tool_name="get-library-docs")

            # Or for internal tools
            get_tool_schema(server_name="gobby-tasks", tool_name="create_task")
        """
        # Handle internal servers locally
        if internal_manager.is_internal(server_name):
            registry = internal_manager.get_registry(server_name)
            if not registry:
                available = ", ".join(r.name for r in internal_manager.get_all_registries())
                return {
                    "success": False,
                    "error": f"Internal server '{server_name}' not found",
                    "available_internal_servers": available,
                }

            schema = registry.get_schema(tool_name)
            if not schema:
                available_tools = [t["name"] for t in registry.list_tools()]
                return {
                    "success": False,
                    "error": f"Tool '{tool_name}' not found on '{server_name}'",
                    "available_tools": available_tools,
                }

            return {"success": True, "tool": schema}

        # Route to HTTP daemon for external servers
        return await _call_daemon_tool(
            daemon_port=daemon_port,
            tool_name="get_tool_schema",
            arguments={
                "server_name": server_name,
                "tool_name": tool_name,
            },
            timeout=10.0,
        )

    @mcp.tool
    async def execute_code(
        code: str,
        language: str = "python",
        context: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """
        Execute code using Claude's code execution sandbox via Claude Agent SDK.

        Uses your Claude subscription (no API costs) to run code in a secure sandbox.
        Perfect for processing large datasets, performing calculations, or data analysis.

        Common use cases:
        - Filter/aggregate large MCP results (e.g., million-row Supabase queries)
        - Data transformations and analysis
        - Mathematical computations
        - Generate visualizations

        Args:
            code: The code to execute (Python only for now)
            language: Programming language (default: "python", only Python supported currently)
            context: Optional context/instructions for Claude about what the code should do
            timeout: Maximum execution time in seconds (default from config)

        Returns:
            Dict with execution results:
            {
                "success": True,
                "result": <execution output>,
                "language": "python",
                "execution_time": <seconds>
            }

        Example - Process large dataset:
            execute_code(
                code="import pandas as pd; df = pd.DataFrame(data); df[df['value'] > 100].head(10).to_dict()",
                context="Filter rows where value > 100 and return top 10 results"
            )

        Example - Data analysis:
            execute_code(
                code="sum(x**2 for x in range(1000))",
                context="Calculate sum of squares from 1 to 1000"
            )
        """
        arguments: dict[str, Any] = {"code": code, "language": language}
        if context:
            arguments["context"] = context
        if timeout is not None:
            arguments["timeout"] = timeout

        return await _call_daemon_tool(
            daemon_port=daemon_port,
            tool_name="execute_code",
            arguments=arguments,
            timeout=max(timeout or 30, 30) + 5,  # Add 5s buffer to daemon timeout
        )

    @mcp.tool
    async def process_large_dataset(
        data: list[dict[str, Any]] | dict[str, Any],
        operation: str,
        parameters: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """
        Process large datasets using Claude's code execution for token optimization.

        Perfect for handling large MCP results (like million-row Supabase queries) by
        processing them in a sandbox before returning to Claude, saving massive token costs.

        Uses your Claude subscription - no API costs beyond what you're already paying.

        Args:
            data: Dataset to process (list of dicts or single dict)
            operation: What to do with the data, in natural language
                      Examples:
                      - "Filter rows where value > 100 and return top 10"
                      - "Group by user_id and sum the amounts"
                      - "Calculate average, min, max for the 'score' field"
                      - "Extract unique email addresses"
            parameters: Optional dict of parameters to use in processing
                       Example: {"threshold": 100, "limit": 10}
            timeout: Maximum execution time in seconds (default from config)

        Returns:
            Dict with processed results:
            {
                "success": True,
                "result": <processed data>,
                "original_size": <input row count>,
                "processed_size": <output row count>,
                "reduction": <percentage reduction>,
                "execution_time": <seconds>
            }

        Example - Filter large Supabase result:
            # Instead of sending 1M rows to Claude (500k tokens)
            # Process it first (returns 100 rows, ~50 tokens)
            process_large_dataset(
                data=supabase_result,
                operation="Filter users who logged in within last 7 days and are premium subscribers",
                parameters={"days": 7}
            )

        Example - Aggregate sales data:
            process_large_dataset(
                data=sales_data,
                operation="Group by product_id and calculate total revenue and count",
            )
        """
        arguments: dict[str, Any] = {"data": data, "operation": operation}
        if parameters:
            arguments["parameters"] = parameters
        if timeout is not None:
            arguments["timeout"] = timeout

        return await _call_daemon_tool(
            daemon_port=daemon_port,
            tool_name="process_large_dataset",
            arguments=arguments,
            timeout=max(timeout or 30, 30) + 5,  # Add 5s buffer to daemon timeout
        )

    # ===== RESOURCES =====

    @mcp.resource("gobby://daemon/status")
    async def daemon_status_resource() -> dict[str, Any]:
        """
        Daemon status as a resource.

        Provides read-only access to daemon status information.
        """
        return await _get_status()

    logger.debug("✅ Stdio MCP wrapper created with daemon lifecycle tools")
    return mcp


async def ensure_daemon_running() -> None:
    """
    Ensure daemon is running, start it if not.

    This is called on stdio MCP server startup to ensure
    the daemon is available for proxying.
    """
    config = load_config()
    daemon_port = config.daemon_port

    if is_daemon_running():
        # Check if it's healthy
        if await check_daemon_http_health(daemon_port):
            logger.debug("✅ Daemon is already running and healthy")
            return
        else:
            logger.warning("⚠️ Daemon is running but not healthy, restarting...")
            await restart_daemon_process()
    else:
        logger.debug("🚀 Starting daemon...")
        result = await start_daemon_process()
        if not result.get("success"):
            error_msg = result.get("message")
            stderr_output = result.get("error", "")
            logger.error(f"❌ Failed to start daemon: {error_msg}")
            if stderr_output:
                logger.error(f"Daemon stderr: {stderr_output}")
            sys.exit(1)

    # Wait for daemon to be healthy
    logger.debug("⏳ Waiting for daemon to be healthy...")
    for _ in range(10):
        if await check_daemon_http_health(daemon_port):
            logger.debug("✅ Daemon is healthy")
            return
        await asyncio.sleep(1)

    logger.error("❌ Daemon failed to become healthy")
    sys.exit(1)


async def main() -> None:
    """Main entry point for stdio MCP server."""
    try:
        # Config log path
        log_file = Path.home() / ".gobby" / "logs" / "mcp-debug.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        # Setup logging to file AND stderr
        # Root logger at WARNING to silence noise from libs like docket/httpcore
        logging.basicConfig(
            level=logging.WARNING,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stderr)],
        )

        # Set gobby (our package) logger to DEBUG
        logging.getLogger("gobby").setLevel(logging.DEBUG)
        logging.getLogger("__main__").setLevel(logging.DEBUG)

        # Aggressively silence all other loggers
        for name in logging.root.manager.loggerDict:
            if not name.startswith("gobby") and name != "__main__":
                logging.getLogger(name).setLevel(logging.WARNING)

        # Explicitly silence known noisy loggers that may be created later
        # docket.worker polls every 250ms and logs 3 DEBUG lines per poll
        logging.getLogger("docket").setLevel(logging.WARNING)
        logging.getLogger("docket.worker").setLevel(logging.WARNING)

        logger.info("Starting MCP server...")

        # Ensure daemon is running
        await ensure_daemon_running()

        # Create and run stdio MCP server
        mcp = create_stdio_mcp_server()

        # Note: The daemon doesn't expose a standard MCP HTTP endpoint.
        # It only has custom proxy endpoints for external MCP servers.
        # The stdio wrapper provides lifecycle management tools instead.

        # Run stdio MCP server
        # Use run_async() since we're already in an async context
        # Suppress banner to avoid interfering with MCP protocol on stdout
        await mcp.run_async(transport="stdio", show_banner=False)
    except Exception as e:
        # Catch and log any startup crashes to stderr
        import traceback

        sys.stderr.write(f"CRITICAL ERROR: MCP server crashed: {e}\n")
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
