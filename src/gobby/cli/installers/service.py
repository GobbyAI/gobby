"""
OS-level service installation for the Gobby daemon.

Handles writing platform-native service configs (launchd on macOS,
systemd on Linux) so the daemon starts automatically on boot.
"""

import logging
import os
import re
import subprocess  # nosec B404 # subprocess needed for launchctl/systemctl
import sys
import time
from pathlib import Path
from typing import Any

from gobby.cli.installers.service_common import (
    LAUNCHD_LABEL,
    LAUNCHD_PLIST_NAME,
    SYSTEMD_UNIT_NAME,
    _build_path,
    _ensure_cli_on_path,
    _find_project_from_cwd,
    _find_project_root,
    _is_dev_mode,
    _render_template,
    _resolve_install_context,
)
from gobby.cli.installers.service_linux import (
    _check_linger,
    _get_service_status_linux,
    _linux_restart,
    _linux_start,
    _linux_stop,
    _systemd_unit_path,
    disable_service_linux,
    enable_service_linux,
    install_service_linux,
    uninstall_service_linux,
)

logger = logging.getLogger(__name__)

__all__ = [
    "LAUNCHD_LABEL",
    "LAUNCHD_PLIST_NAME",
    "SYSTEMD_UNIT_NAME",
    "_build_path",
    "_check_linger",
    "_ensure_cli_on_path",
    "_find_project_from_cwd",
    "_find_project_root",
    "_get_service_status_linux",
    "_is_dev_mode",
    "_linux_restart",
    "_linux_start",
    "_linux_stop",
    "_render_template",
    "_resolve_install_context",
    "_systemd_unit_path",
    "disable_service",
    "disable_service_linux",
    "disable_service_macos",
    "enable_service",
    "enable_service_linux",
    "enable_service_macos",
    "get_service_status",
    "install_service",
    "install_service_linux",
    "install_service_macos",
    "service_restart",
    "service_start",
    "service_stop",
    "uninstall_service",
    "uninstall_service_linux",
    "uninstall_service_macos",
]


# ---------------------------------------------------------------------------
# macOS (launchd)
# ---------------------------------------------------------------------------


def _plist_path() -> Path:
    """Return the launchd plist file path."""
    return Path.home() / "Library" / "LaunchAgents" / LAUNCHD_PLIST_NAME


def install_service_macos(*, verbose: bool = False) -> dict[str, Any]:
    """Install the Gobby daemon as a macOS launchd user agent.

    Writes the plist to ~/Library/LaunchAgents/ and bootstraps it.
    """
    ctx = _resolve_install_context(verbose=verbose)
    plist_content = _render_template(
        "com.gobby.daemon.plist.j2",
        **ctx,
    )

    plist_file = _plist_path()
    plist_file.parent.mkdir(parents=True, exist_ok=True)

    # If already loaded, bootout first (ignore errors if not loaded)
    _launchctl_bootout(quiet=True)

    plist_file.write_text(plist_content, encoding="utf-8")
    plist_file.chmod(0o644)

    # Bootstrap the service
    uid = os.getuid()
    result = subprocess.run(  # nosec B603 B607
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_file)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        # Error 37 = "service already loaded" — not a real failure
        if "37:" not in (result.stderr or ""):
            return {
                "success": False,
                "error": f"launchctl bootstrap failed: {result.stderr or result.stdout}",
                "plist_file": str(plist_file),
            }

    result_dict: dict[str, Any] = {
        "success": True,
        "plist_file": str(plist_file),
        "platform": "macos",
        **ctx,
    }

    if ctx["mode"] == "dev":
        cli_result = _ensure_cli_on_path(str(ctx["working_directory"]))
        result_dict.update(cli_result)

    return result_dict


def uninstall_service_macos() -> dict[str, Any]:
    """Uninstall the Gobby daemon launchd user agent."""
    plist_file = _plist_path()

    _launchctl_bootout(quiet=False)

    if plist_file.exists():
        plist_file.unlink()

    return {
        "success": True,
        "plist_file": str(plist_file),
        "platform": "macos",
    }


def enable_service_macos() -> dict[str, Any]:
    """Re-enable the launchd service after disable (bootstrap it).

    Checks daemon health before touching launchd.  If the service is
    already running, returns immediately — no bootout/bootstrap cycle.
    """
    plist_file = _plist_path()
    if not plist_file.exists():
        return {
            "success": False,
            "error": "Service not installed. Run `gobby service install` first.",
        }

    # Check if the daemon is already running and healthy.
    # Blindly booting out a healthy daemon causes SIGTERM without graceful
    # shutdown, triggering restart loops when called repeatedly (#10680).
    status = _get_service_status_macos()
    if status.get("running"):
        logger.debug(
            f"Daemon already running (pid={status.get('pid')}) - skipping bootout/bootstrap"
        )
        return {"success": True, "platform": "macos", "already_running": True}

    # Bootout any stale service entry before bootstrapping.
    # Without this, bootstrap fails with error 5 (I/O error) when a
    # previous service entry exists but the process is dead.
    if status.get("enabled"):
        _launchctl_bootout(quiet=True)

    uid = os.getuid()
    result = subprocess.run(  # nosec B603 B607
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_file)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0 and "37:" not in (result.stderr or ""):
        return {
            "success": False,
            "error": f"launchctl bootstrap failed: {result.stderr or result.stdout}",
        }

    return {"success": True, "platform": "macos"}


def disable_service_macos() -> dict[str, Any]:
    """Temporarily stop the launchd service without uninstalling."""
    plist_file = _plist_path()
    if not plist_file.exists():
        return {"success": False, "error": "Service not installed."}

    result = _launchctl_bootout(quiet=False)
    if not result.get("success"):
        return result
    return {"success": True, "platform": "macos"}


def _launchctl_bootout(*, quiet: bool) -> dict[str, Any]:
    """Bootout the launchd service (stop + unload)."""
    uid = os.getuid()
    try:
        result = subprocess.run(  # nosec B603 B607
            ["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (result.stderr or result.stdout or "").strip()
        if result.returncode == 0:
            return {"success": True, "platform": "macos"}
        if _launchctl_bootout_already_unloaded(output):
            if not quiet:
                logger.debug("launchctl bootout: service already unloaded: %s", output)
            return {"success": True, "platform": "macos", "already_unloaded": True}
        error = f"launchctl bootout failed: {output or f'exit code {result.returncode}'}"
        if not quiet:
            logger.warning(error)
        return {"success": False, "platform": "macos", "error": error}
    except subprocess.TimeoutExpired:
        error = "launchctl bootout timed out"
        if not quiet:
            logger.warning(error)
        return {"success": False, "platform": "macos", "error": error}
    except OSError as e:
        error = f"launchctl bootout failed: {e}"
        if not quiet:
            logger.warning(error)
        return {"success": False, "platform": "macos", "error": error}


def _launchctl_bootout_already_unloaded(output: str) -> bool:
    """Return whether launchctl bootout failed because the service is already unloaded."""
    message = output.lower()
    markers = ("no such process", "could not find specified service", "service is not loaded")
    return any(marker in message for marker in markers)


def _get_service_status_macos() -> dict[str, Any]:
    """Get macOS launchd service status."""
    plist_file = _plist_path()
    installed = plist_file.exists()

    if not installed:
        return {"installed": False, "enabled": False, "running": False, "platform": "macos"}

    # Check if loaded and running via launchctl print
    uid = os.getuid()
    try:
        result = subprocess.run(  # nosec B603 B607
            ["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        loaded = result.returncode == 0
        running = False
        pid = None
        if loaded and result.stdout:
            for raw_line in result.stdout.splitlines():
                stripped = raw_line.strip()
                # Only match top-level keys (single tab indent) to avoid
                # nested subprocess `state = active` overwriting the
                # service-level `state = running`.
                is_top_level = raw_line.startswith("\t") and not raw_line.startswith("\t\t")
                if stripped.startswith("pid = ") and is_top_level:
                    try:
                        pid = int(stripped.split("=")[1].strip())
                        running = True
                    except (ValueError, IndexError):
                        pass
                elif stripped.startswith("state = ") and is_top_level:
                    state = stripped.split("=")[1].strip()
                    running = state == "running"
    except (subprocess.TimeoutExpired, OSError):
        loaded = False
        running = False
        pid = None

    # Validate baked-in paths
    warnings = _validate_plist_paths(plist_file)

    status: dict[str, Any] = {
        "installed": True,
        "enabled": loaded,
        "running": running,
        "platform": "macos",
        "plist_file": str(plist_file),
    }
    if pid is not None:
        status["pid"] = pid
    if warnings:
        status["warnings"] = warnings

    # Detect mode from plist content
    try:
        content = plist_file.read_text(encoding="utf-8")
        if "pyproject.toml" not in content:
            # Check if working directory looks like a project
            for line in content.splitlines():
                if "<string>" in line and "Projects" in line:
                    status["mode"] = "dev"
                    break
            else:
                status["mode"] = "installed"
        else:
            status["mode"] = "installed"
        # Better mode detection: check if the python path is in a .venv
        if ".venv" in content:
            status["mode"] = "dev"
    except OSError:
        pass

    return status


def _validate_plist_paths(plist_file: Path) -> list[str]:
    """Check that paths baked into the plist still exist."""
    warnings = []
    try:
        content = plist_file.read_text(encoding="utf-8")
        # Extract ProgramArguments first <string> (python executable)
        exe_match = re.search(
            r"<key>ProgramArguments</key>\s*<array>\s*<string>([^<]+)</string>",
            content,
        )
        if exe_match:
            exe_path = Path(exe_match.group(1))
            if not exe_path.exists():
                warnings.append(f"Python executable not found: {exe_path}")

        # Extract WorkingDirectory
        wd_match = re.search(
            r"<key>WorkingDirectory</key>\s*<string>([^<]+)</string>",
            content,
        )
        if wd_match:
            wd_path = Path(wd_match.group(1))
            if not wd_path.exists():
                warnings.append(f"Working directory not found: {wd_path}")
    except OSError:
        pass
    return warnings


def _macos_restart() -> dict[str, Any]:
    """Restart the macOS service via bootout + bootstrap.

    A full bootout/bootstrap cycle ensures launchd re-reads the plist
    and spawns a fresh process, picking up any new code.  The previous
    ``kickstart -k`` approach reused the cached service registration
    which silently ignored code changes.
    """
    plist_file = _plist_path()
    if not plist_file.exists():
        return {
            "success": False,
            "error": "Service not installed. Run `gobby service install` first.",
        }

    uid = os.getuid()

    # Bootout the running service (stop + unload).
    _launchctl_bootout(quiet=True)

    # Wait for launchd to fully unload the service entry.
    # The plist sets ExitTimeOut=60s, so the process may take that long
    # to terminate. Poll with buffer to avoid racing bootstrap.
    for _ in range(750):  # up to ~75s
        status = _get_service_status_macos()
        if not status.get("enabled"):
            break
        time.sleep(0.1)

    # Bootstrap a fresh instance from the plist.
    # Retry on error 5 (I/O error) which means launchd hasn't fully
    # released the domain entry yet despite our polling above.
    max_retries = 5
    last_error = ""
    for attempt in range(max_retries):
        try:
            result = subprocess.run(  # nosec B603 B607
                ["launchctl", "bootstrap", f"gui/{uid}", str(plist_file)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 or "37:" in (result.stderr or ""):
                return {
                    "success": True,
                    "platform": "macos",
                    "method": "launchctl bootout + bootstrap",
                }
            last_error = result.stderr or result.stdout
            # Error 5 = I/O error (domain not yet released) — retry
            if "5:" in last_error and attempt < max_retries - 1:
                time.sleep(1)
                continue
            break
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "launchctl bootstrap timed out"}
        except OSError as e:
            return {"success": False, "error": str(e)}

    return {
        "success": False,
        "error": f"launchctl bootstrap failed: {last_error}",
    }


def _macos_start() -> dict[str, Any]:
    """Start the macOS service via launchctl bootstrap."""
    return enable_service_macos()


def _macos_stop() -> dict[str, Any]:
    """Stop the macOS service via launchctl bootout."""
    return disable_service_macos()


# ---------------------------------------------------------------------------
# Platform dispatch
# ---------------------------------------------------------------------------


def install_service(*, verbose: bool = False) -> dict[str, Any]:
    """Install the Gobby daemon as an OS-level service.

    Auto-detects the platform and writes the appropriate service config.
    """
    if sys.platform == "darwin":
        return install_service_macos(verbose=verbose)
    elif sys.platform == "linux":
        return install_service_linux(verbose=verbose)
    elif sys.platform == "win32":
        from gobby.cli.installers.service_windows import install_service_windows

        return install_service_windows(verbose=verbose)
    else:
        return {
            "success": False,
            "error": f"Unsupported platform: {sys.platform}",
        }


def uninstall_service() -> dict[str, Any]:
    """Uninstall the Gobby daemon OS-level service."""
    if sys.platform == "darwin":
        return uninstall_service_macos()
    elif sys.platform == "linux":
        return uninstall_service_linux()
    elif sys.platform == "win32":
        from gobby.cli.installers.service_windows import uninstall_service_windows

        return uninstall_service_windows()
    else:
        return {"success": False, "error": f"Unsupported platform: {sys.platform}"}


def enable_service() -> dict[str, Any]:
    """Re-enable the OS service after it was disabled."""
    if sys.platform == "darwin":
        return enable_service_macos()
    elif sys.platform == "linux":
        return enable_service_linux()
    elif sys.platform == "win32":
        from gobby.cli.installers.service_windows import enable_service_windows

        return enable_service_windows()
    else:
        return {"success": False, "error": f"Unsupported platform: {sys.platform}"}


def disable_service() -> dict[str, Any]:
    """Temporarily stop the OS service without uninstalling."""
    if sys.platform == "darwin":
        return disable_service_macos()
    elif sys.platform == "linux":
        return disable_service_linux()
    elif sys.platform == "win32":
        from gobby.cli.installers.service_windows import disable_service_windows

        return disable_service_windows()
    else:
        return {"success": False, "error": f"Unsupported platform: {sys.platform}"}


def get_service_status() -> dict[str, Any]:
    """Get the OS service status (installed/enabled/running)."""
    if sys.platform == "darwin":
        return _get_service_status_macos()
    elif sys.platform == "linux":
        return _get_service_status_linux()
    elif sys.platform == "win32":
        from gobby.cli.installers.service_windows import _get_service_status_windows

        return _get_service_status_windows()
    else:
        return {"installed": False, "enabled": False, "running": False, "platform": sys.platform}


def service_restart(shutdown_source: str = "service_restart") -> dict[str, Any]:
    """Restart the daemon through the OS service manager."""
    from gobby.runner_maintenance import write_shutdown_source

    if sys.platform == "darwin":
        restart_fn = _macos_restart
    elif sys.platform == "linux":
        restart_fn = _linux_restart
    elif sys.platform == "win32":
        from gobby.cli.installers.service_windows import _windows_restart

        restart_fn = _windows_restart
    else:
        return {"success": False, "error": f"Unsupported platform: {sys.platform}"}

    try:
        write_shutdown_source(shutdown_source, intent="restart")
    except Exception as e:
        logger.warning("Failed to write shutdown source before service restart: %s", e)
    return restart_fn()


def service_start() -> dict[str, Any]:
    """Start the daemon through the OS service manager."""
    if sys.platform == "darwin":
        return _macos_start()
    elif sys.platform == "linux":
        return _linux_start()
    elif sys.platform == "win32":
        from gobby.cli.installers.service_windows import _windows_start

        return _windows_start()
    else:
        return {"success": False, "error": f"Unsupported platform: {sys.platform}"}


def service_stop(
    *,
    shutdown_intent: str = "stop",
    shutdown_source: str = "service_stop",
) -> dict[str, Any]:
    """Stop the daemon through the OS service manager."""
    from gobby.runner_maintenance import write_shutdown_source

    if sys.platform == "darwin":
        stop_fn = _macos_stop
    elif sys.platform == "linux":
        stop_fn = _linux_stop
    elif sys.platform == "win32":
        from gobby.cli.installers.service_windows import _windows_stop

        stop_fn = _windows_stop
    else:
        return {"success": False, "error": f"Unsupported platform: {sys.platform}"}

    try:
        write_shutdown_source(shutdown_source, intent=shutdown_intent)
    except Exception as e:
        logger.warning("Failed to write shutdown source before service stop: %s", e)
    return stop_fn()
