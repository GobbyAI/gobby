"""
Linux systemd service backend for the Gobby daemon.
"""

import os
import subprocess  # nosec B404 # subprocess needed for systemctl/loginctl
from pathlib import Path
from typing import Any

from gobby.cli.installers.service_common import (
    SYSTEMD_UNIT_NAME,
    _ensure_cli_on_path,
    _render_template,
    _resolve_install_context,
)


def _systemd_unit_path() -> Path:
    """Return the systemd user unit file path."""
    config_home = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(config_home) / "systemd" / "user" / SYSTEMD_UNIT_NAME


def install_service_linux(*, verbose: bool = False) -> dict[str, Any]:
    """Install the Gobby daemon as a systemd user service."""
    ctx = _resolve_install_context(verbose=verbose)
    unit_content = _render_template(
        "gobby-daemon.service.j2",
        **ctx,
    )

    unit_file = _systemd_unit_path()
    unit_file.parent.mkdir(parents=True, exist_ok=True)
    unit_file.write_text(unit_content, encoding="utf-8")

    # Reload systemd, enable, and start
    cmds = [
        (["systemctl", "--user", "daemon-reload"], "daemon-reload"),
        (["systemctl", "--user", "enable", SYSTEMD_UNIT_NAME], "enable"),
        (["systemctl", "--user", "start", SYSTEMD_UNIT_NAME], "start"),
    ]

    for cmd, label in cmds:
        try:
            result = subprocess.run(  # nosec B603 B607
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return {
                    "success": False,
                    "error": f"systemctl {label} failed: {result.stderr or result.stdout}",
                    "unit_file": str(unit_file),
                }
        except (subprocess.TimeoutExpired, OSError) as e:
            return {"success": False, "error": f"systemctl {label} failed: {e}"}

    # Check linger
    warnings = _check_linger()

    result_dict: dict[str, Any] = {
        "success": True,
        "unit_file": str(unit_file),
        "platform": "linux",
        **ctx,
    }
    if warnings:
        result_dict["warnings"] = warnings

    if ctx["mode"] == "dev":
        cli_result = _ensure_cli_on_path(str(ctx["working_directory"]))
        result_dict.update(cli_result)

    return result_dict


def uninstall_service_linux() -> dict[str, Any]:
    """Uninstall the Gobby daemon systemd user service."""
    unit_file = _systemd_unit_path()

    # Stop and disable
    for action in ["stop", "disable"]:
        try:
            subprocess.run(  # nosec B603 B607
                ["systemctl", "--user", action, SYSTEMD_UNIT_NAME],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass

    if unit_file.exists():
        unit_file.unlink()

    # Reload
    try:
        subprocess.run(  # nosec B603 B607
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass

    return {
        "success": True,
        "unit_file": str(unit_file),
        "platform": "linux",
    }


def enable_service_linux() -> dict[str, Any]:
    """Re-enable and start the systemd service."""
    unit_file = _systemd_unit_path()
    if not unit_file.exists():
        return {
            "success": False,
            "error": "Service not installed. Run `gobby service install` first.",
        }

    for action in ["enable", "start"]:
        try:
            result = subprocess.run(  # nosec B603 B607
                ["systemctl", "--user", action, SYSTEMD_UNIT_NAME],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return {
                    "success": False,
                    "error": f"systemctl {action} failed: {result.stderr or result.stdout}",
                }
        except (subprocess.TimeoutExpired, OSError) as e:
            return {"success": False, "error": f"systemctl {action} failed: {e}"}

    return {"success": True, "platform": "linux"}


def disable_service_linux() -> dict[str, Any]:
    """Temporarily stop the systemd service without uninstalling."""
    unit_file = _systemd_unit_path()
    if not unit_file.exists():
        return {"success": False, "error": "Service not installed."}

    try:
        result = subprocess.run(  # nosec B603 B607
            ["systemctl", "--user", "stop", SYSTEMD_UNIT_NAME],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {
                "success": False,
                "error": f"systemctl stop failed: {result.stderr or result.stdout}",
            }
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": str(e)}

    return {"success": True, "platform": "linux"}


def _get_service_status_linux() -> dict[str, Any]:
    """Get Linux systemd service status."""
    unit_file = _systemd_unit_path()
    installed = unit_file.exists()

    if not installed:
        return {"installed": False, "enabled": False, "running": False, "platform": "linux"}

    enabled = False
    running = False
    pid = None

    try:
        result = subprocess.run(  # nosec B603 B607
            ["systemctl", "--user", "is-enabled", SYSTEMD_UNIT_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        enabled = result.stdout.strip() == "enabled"
    except (subprocess.TimeoutExpired, OSError):
        pass

    try:
        result = subprocess.run(  # nosec B603 B607
            ["systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        running = result.stdout.strip() == "active"
    except (subprocess.TimeoutExpired, OSError):
        pass

    if running:
        try:
            result = subprocess.run(  # nosec B603 B607
                ["systemctl", "--user", "show", SYSTEMD_UNIT_NAME, "--property=MainPID"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            pid_str = result.stdout.strip().replace("MainPID=", "")
            if pid_str and pid_str != "0":
                pid = int(pid_str)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass

    warnings = _check_linger()

    status: dict[str, Any] = {
        "installed": True,
        "enabled": enabled,
        "running": running,
        "platform": "linux",
        "unit_file": str(unit_file),
    }
    if pid is not None:
        status["pid"] = pid
    if warnings:
        status["warnings"] = warnings

    # Detect mode
    try:
        content = unit_file.read_text(encoding="utf-8")
        status["mode"] = "dev" if ".venv" in content else "installed"
    except OSError:
        pass

    return status


def _check_linger() -> list[str]:
    """Check if loginctl linger is enabled (required for boot-start without login)."""
    warnings = []
    try:
        user = os.environ.get("USER", "")
        if not user:
            import getpass

            try:
                user = getpass.getuser()
            except (KeyError, OSError):
                pass
        if not user:
            warnings.append("Could not determine username — skipping linger check")
            return warnings
        result = subprocess.run(  # nosec B603 B607
            ["loginctl", "show-user", user, "--property=Linger"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and "Linger=no" in result.stdout:
            warnings.append(
                f"Linger not enabled. Service won't start at boot without login. "
                f"Run: loginctl enable-linger {user}"
            )
    except (subprocess.TimeoutExpired, OSError):
        pass  # loginctl not available or timed out
    return warnings


def _linux_restart() -> dict[str, Any]:
    """Restart the Linux service using systemctl restart."""
    try:
        result = subprocess.run(  # nosec B603 B607
            ["systemctl", "--user", "restart", SYSTEMD_UNIT_NAME],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {
                "success": False,
                "error": f"systemctl restart failed: {result.stderr or result.stdout}",
            }
        return {"success": True, "platform": "linux", "method": "systemctl restart"}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": str(e)}


def _linux_start() -> dict[str, Any]:
    """Start the Linux service."""
    return enable_service_linux()


def _linux_stop() -> dict[str, Any]:
    """Stop the Linux service."""
    return disable_service_linux()
