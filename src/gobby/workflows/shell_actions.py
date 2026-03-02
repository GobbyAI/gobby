from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def handle_shell_run(context: Any, **kwargs: Any) -> dict[str, Any] | None:
    """Handle the 'shell' workflow action.

    Executes shell commands using the system's default shell:
    - Unix/macOS: /bin/sh (typically bash or dash)
    - Windows: cmd.exe

    Args:
        context: Action execution context
        **kwargs: Action parameters
            command: Shell command to execute (can contain Jinja2 templates)
            background: Whether to run in background (default: False)
            capture_output: Whether to capture stdout/stderr (default: True, ignored if background=True)
            cwd: Working directory for command (optional)

    Returns:
        Dict with execution results (stdout, stderr, exit_code, or pid)

    Note:
        Shell syntax varies by platform. For cross-platform compatibility,
        use simple commands or check sys.platform for platform-specific syntax.
    """
    command_template = kwargs.get("command")
    if not command_template:
        logger.warning("Missing 'command' parameter for shell action")
        return {"error": "Missing 'command' parameter for shell action"}

    background = kwargs.get("background", False)
    capture_output = kwargs.get("capture_output", True)
    cwd = kwargs.get("cwd")

    # Prepare template context
    variables = context.state.variables if context.state else {}
    render_context: dict[str, Any] = {
        "variables": variables,
        "session_id": context.session_id,
    }

    # If event_data is present (e.g. from hook), include it
    if context.event_data:
        render_context["event"] = context.event_data

    # Fetch session object for template access (e.g., session.seq_num)
    session = None
    if context.session_manager and context.session_id:
        loop = asyncio.get_running_loop()
        session = await loop.run_in_executor(None, context.session_manager.get, context.session_id)
    if session:
        render_context["session"] = session

    # Fetch project object for template access (e.g., project.name)
    if session and session.project_id and context.db:
        try:
            from gobby.storage.projects import LocalProjectManager

            loop = asyncio.get_running_loop()
            project_mgr = LocalProjectManager(context.db)
            project = await loop.run_in_executor(None, project_mgr.get, session.project_id)
            if project:
                render_context["project"] = project
        except Exception as e:
            logger.warning(f"Failed to fetch project for template context: {e}")

    try:
        command = context.template_engine.render(command_template, render_context)
    except Exception as e:
        logger.error(f"Error rendering bash command template: {e}")
        return {"error": f"Template rendering failed: {str(e)}"}

    logger.info(f"Executing bash command: {command}")

    try:
        if background:
            # Run in background (don't wait)
            # Use shell=True to support pipes, redirects, etc.
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=cwd,
            )

            return {"status": "started", "pid": proc.pid}
        else:
            # Run and wait for completion
            subprocess_kwargs: dict[str, Any] = {
                "cwd": cwd,
            }

            if capture_output:
                subprocess_kwargs["stdout"] = asyncio.subprocess.PIPE
                subprocess_kwargs["stderr"] = asyncio.subprocess.PIPE
            else:
                subprocess_kwargs["stdout"] = asyncio.subprocess.DEVNULL
                subprocess_kwargs["stderr"] = asyncio.subprocess.DEVNULL

            # Use shell=True
            proc = await asyncio.create_subprocess_shell(command, **subprocess_kwargs)

            if capture_output:
                stdout_bytes, stderr_bytes = await proc.communicate()
                stdout = stdout_bytes.decode("utf-8", errors="replace")
                stderr = stderr_bytes.decode("utf-8", errors="replace")
            else:
                await proc.wait()
                stdout = ""
                stderr = ""

            return {
                "stdout": stdout.strip(),
                "stderr": stderr.strip(),
                "exit_code": proc.returncode if proc.returncode is not None else 0,
            }

    except Exception as e:
        logger.error(f"Shell command execution failed: {e}", exc_info=True)
        return {"error": str(e), "exit_code": 1}
