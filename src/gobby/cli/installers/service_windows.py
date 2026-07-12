"""
Windows service support for the Gobby daemon via Task Scheduler.

Uses schtasks.exe to register a scheduled task that starts the daemon
at user logon, providing equivalent functionality to launchd (macOS)
and systemd (Linux) backends.
"""

import logging
import subprocess  # nosec B404 # subprocess needed for schtasks
from pathlib import Path
from typing import Any

from gobby.paths import get_gobby_home

logger = logging.getLogger(__name__)

# Task Scheduler identifiers
WINDOWS_TASK_NAME = "GobbyDaemon"
WINDOWS_TASK_XML_NAME = "gobby-daemon.task.xml"
WINDOWS_LAUNCHER_NAME = "gobby-launcher.cmd"


def _task_xml_path() -> Path:
    """Return the path to the Task Scheduler XML file."""
    return get_gobby_home() / WINDOWS_TASK_XML_NAME


def _launcher_script_path() -> Path:
    """Return the path to the launcher batch script."""
    return get_gobby_home() / WINDOWS_LAUNCHER_NAME


def _run_schtasks(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run a schtasks command and return the result.

    Args:
        args: Arguments to pass after 'schtasks'.
        timeout: Command timeout in seconds.

    Returns:
        CompletedProcess with stdout/stderr.
    """
    cmd = ["schtasks", *args]
    return subprocess.run(  # nosec B603 B607 # hardcoded schtasks command
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Install / Uninstall
# ---------------------------------------------------------------------------


def install_service_windows(*, verbose: bool = False) -> dict[str, Any]:
    """Install the Gobby daemon as a Windows scheduled task.

    Creates a launcher batch script and Task Scheduler XML definition,
    then registers the task via schtasks /create.
    """
    from gobby.cli.installers.service import _render_template, _resolve_install_context

    ctx = _resolve_install_context(verbose=verbose)

    gobby_dir = get_gobby_home()
    gobby_dir.mkdir(parents=True, exist_ok=True)

    # Write launcher batch script
    launcher_path = _launcher_script_path()
    launcher_content = _render_template("gobby-launcher.cmd.j2", **ctx)
    launcher_path.write_text(launcher_content, encoding="utf-8")

    # Write Task Scheduler XML with launcher path baked in
    task_xml_path = _task_xml_path()
    xml_content = _render_template(
        "gobby-daemon.task.xml.j2",
        launcher_script=str(launcher_path),
        working_directory=ctx["working_directory"],
    )
    task_xml_path.write_text(xml_content, encoding="utf-8")

    # Register the scheduled task
    try:
        result = _run_schtasks(
            [
                "/create",
                "/tn",
                WINDOWS_TASK_NAME,
                "/xml",
                str(task_xml_path),
                "/f",
            ]
        )
        if result.returncode != 0:
            return {
                "success": False,
                "error": f"schtasks /create failed: {result.stderr or result.stdout}",
                "task_file": str(task_xml_path),
            }
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": f"schtasks /create failed: {e}"}

    # Start the task immediately
    try:
        _run_schtasks(["/run", "/tn", WINDOWS_TASK_NAME])
    except (subprocess.TimeoutExpired, OSError):
        pass  # Non-fatal — task will start at next logon

    result_dict: dict[str, Any] = {
        "success": True,
        "task_file": str(task_xml_path),
        "launcher_script": str(launcher_path),
        "platform": "windows",
        **ctx,
    }

    if ctx["mode"] == "dev":
        from gobby.cli.installers.service import _ensure_cli_on_path

        cli_result = _ensure_cli_on_path(str(ctx["working_directory"]))
        result_dict.update(cli_result)

    return result_dict


def uninstall_service_windows() -> dict[str, Any]:
    """Uninstall the Gobby daemon scheduled task."""
    # Stop the running task first (ignore errors)
    try:
        _run_schtasks(["/end", "/tn", WINDOWS_TASK_NAME])
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Delete the scheduled task
    try:
        result = _run_schtasks(["/delete", "/tn", WINDOWS_TASK_NAME, "/f"])
        if result.returncode != 0:
            # Task may not exist — check if it's a "not found" error
            stderr = result.stderr or result.stdout
            if "does not exist" not in stderr.lower() and "cannot find" not in stderr.lower():
                return {
                    "success": False,
                    "error": f"schtasks /delete failed: {stderr}",
                }
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": f"schtasks /delete failed: {e}"}

    # Clean up files
    task_xml_path = _task_xml_path()
    launcher_path = _launcher_script_path()
    for path in [task_xml_path, launcher_path]:
        if path.exists():
            path.unlink()

    return {
        "success": True,
        "task_file": str(task_xml_path),
        "platform": "windows",
    }


# ---------------------------------------------------------------------------
# Enable / Disable
# ---------------------------------------------------------------------------


def enable_service_windows() -> dict[str, Any]:
    """Re-enable and start the scheduled task."""
    task_xml_path = _task_xml_path()
    if not task_xml_path.exists():
        return {
            "success": False,
            "error": "Service not installed. Run `gobby service install` first.",
        }

    try:
        result = _run_schtasks(["/change", "/tn", WINDOWS_TASK_NAME, "/enable"])
        if result.returncode != 0:
            return {
                "success": False,
                "error": f"schtasks /change /enable failed: {result.stderr or result.stdout}",
            }
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": f"schtasks /change /enable failed: {e}"}

    try:
        result = _run_schtasks(["/run", "/tn", WINDOWS_TASK_NAME])
        if result.returncode != 0:
            return {
                "success": False,
                "error": f"schtasks /run failed: {result.stderr or result.stdout}",
            }
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": f"schtasks /run failed: {e}"}

    return {"success": True, "platform": "windows"}


def disable_service_windows() -> dict[str, Any]:
    """Stop the scheduled task and disable it."""
    task_xml_path = _task_xml_path()
    if not task_xml_path.exists():
        return {"success": False, "error": "Service not installed."}

    # Stop the running task
    try:
        _run_schtasks(["/end", "/tn", WINDOWS_TASK_NAME])
    except (subprocess.TimeoutExpired, OSError):
        pass  # May not be running

    # Disable the task
    try:
        result = _run_schtasks(["/change", "/tn", WINDOWS_TASK_NAME, "/disable"])
        if result.returncode != 0:
            return {
                "success": False,
                "error": f"schtasks /change /disable failed: {result.stderr or result.stdout}",
            }
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": str(e)}

    return {"success": True, "platform": "windows"}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _get_service_status_windows() -> dict[str, Any]:
    """Get Windows scheduled task status."""
    task_xml_path = _task_xml_path()
    installed = task_xml_path.exists()

    if not installed:
        return {"installed": False, "enabled": False, "running": False, "platform": "windows"}

    enabled = False
    running = False

    try:
        result = _run_schtasks(
            [
                "/query",
                "/tn",
                WINDOWS_TASK_NAME,
                "/fo",
                "LIST",
                "/v",
            ],
            timeout=10,
        )

        if result.returncode == 0:
            output = result.stdout
            for line in output.splitlines():
                line = line.strip()
                # "Scheduled Task State:" line indicates enabled/disabled
                if line.startswith("Scheduled Task State:"):
                    value = line.split(":", 1)[1].strip().lower()
                    enabled = value == "enabled"
                # "Status:" line indicates running state
                elif line.startswith("Status:"):
                    value = line.split(":", 1)[1].strip().lower()
                    running = value == "running"
    except (subprocess.TimeoutExpired, OSError):
        pass

    status: dict[str, Any] = {
        "installed": True,
        "enabled": enabled,
        "running": running,
        "platform": "windows",
        "task_file": str(task_xml_path),
    }

    # Detect mode
    launcher_path = _launcher_script_path()
    try:
        content = launcher_path.read_text(encoding="utf-8")
        status["mode"] = "dev" if ".venv" in content else "installed"
    except OSError:
        pass

    return status


# ---------------------------------------------------------------------------
# Start / Stop / Restart
# ---------------------------------------------------------------------------


def _windows_restart() -> dict[str, Any]:
    """Restart the daemon by ending and re-running the scheduled task."""
    # Stop first
    try:
        _run_schtasks(["/end", "/tn", WINDOWS_TASK_NAME])
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Start
    try:
        result = _run_schtasks(["/run", "/tn", WINDOWS_TASK_NAME])
        if result.returncode != 0:
            return {
                "success": False,
                "error": f"schtasks /run failed: {result.stderr or result.stdout}",
            }
        return {"success": True, "platform": "windows", "method": "schtasks restart"}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": str(e)}


def _windows_start() -> dict[str, Any]:
    """Start the Windows service."""
    return enable_service_windows()


def _windows_stop() -> dict[str, Any]:
    """Stop the Windows service."""
    return disable_service_windows()
