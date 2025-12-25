"""
Status message formatting for Gobby daemon.

Provides consistent status display across CLI and MCP server.
"""

from typing import Any


def format_status_message(
    *,
    running: bool,
    pid: int | None = None,
    pid_file: str | None = None,
    log_files: str | None = None,
    uptime: str | None = None,
    http_port: int | None = None,
    websocket_port: int | None = None,
    **kwargs: Any,
) -> str:
    """
    Format Gobby daemon status message with consistent styling.

    Args:
        running: Whether the daemon is running
        pid: Process ID
        pid_file: Path to PID file
        log_files: Path to log files directory
        uptime: Formatted uptime string (e.g., "1h 23m 45s")
        http_port: HTTP server port
        websocket_port: WebSocket server port

    Returns:
        Formatted status message string
    """
    lines = []

    # Header
    lines.append("=" * 70)
    lines.append("GOBBY DAEMON STATUS")
    lines.append("=" * 70)
    lines.append("")

    # Status section
    if running:
        lines.append("Status: Running")
        if pid:
            lines.append(f"  PID: {pid}")
        if uptime:
            lines.append(f"  Uptime: {uptime}")
        if pid_file:
            lines.append(f"  PID file: {pid_file}")
        if log_files:
            lines.append(f"  Log files: {log_files}")
    else:
        lines.append("Status: Stopped")

    lines.append("")

    # Server Configuration section
    if http_port or websocket_port:
        lines.append("Server Configuration:")
        if http_port:
            lines.append(f"  HTTP Port: {http_port}")
        if websocket_port:
            lines.append(f"  WebSocket Port: {websocket_port}")
        lines.append("")

    # Footer
    lines.append("=" * 70)

    return "\n".join(lines)
