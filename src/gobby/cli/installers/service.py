"""
OS-level service installation for the Gobby daemon.

Handles writing platform-native service configs (launchd on macOS,
systemd on Linux) so the daemon starts automatically on boot.
"""

import logging
import os
import re
import shutil
import subprocess  # nosec B404 # subprocess needed for launchctl/systemctl
import sys
import time
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

# Template directory
_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "install" / "shared" / "services"

# Service identifiers
LAUNCHD_LABEL = "com.gobby.daemon"
LAUNCHD_PLIST_NAME = f"{LAUNCHD_LABEL}.plist"
SYSTEMD_UNIT_NAME = "gobby-daemon.service"


def _render_template(template_name: str, **context: Any) -> str:
    """Render a Jinja2 template from the services directory."""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    template = env.get_template(template_name)
    return template.render(**context)


def _is_dev_mode() -> bool:
    """Check if running from a development install (source checkout with .venv).

    Returns True if:
    1. sys.executable is inside a gobby project directory, OR
    2. CWD is a gobby project directory with a .venv (covers globally
       installed CLI being run from the source checkout)

    Note: Uses has_gobby_pyproject (weaker check) rather than is_dev_mode()
    because service installation needs to work before the source tree is
    fully built (e.g., fresh checkout before first build).
    """
    from gobby.utils.dev import has_gobby_pyproject

    # Strategy 1: Check if sys.executable is inside a gobby project
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if has_gobby_pyproject(parent):
            return True
        if (parent / "pyproject.toml").exists():
            break  # Only check the first pyproject.toml we find

    # Strategy 2: Check if CWD is a gobby project with a .venv
    return _find_project_from_cwd() is not None


def _venv_python(project_root: Path) -> Path:
    """Return the venv python path, platform-aware."""
    if sys.platform == "win32":
        return project_root / ".venv" / "Scripts" / "python.exe"
    return project_root / ".venv" / "bin" / "python3"


def _find_project_from_cwd() -> Path | None:
    """Find a gobby project root from CWD (or parents).

    Returns the project root if CWD is inside a gobby source checkout
    that has a .venv with a python3 executable. Returns None otherwise.

    Note: Uses has_gobby_pyproject (weaker check) rather than
    is_gobby_project() because service installation needs to work even
    when src/gobby/install/shared/ doesn't exist yet.
    """
    from gobby.utils.dev import has_gobby_pyproject

    cwd = Path.cwd().resolve()
    for directory in [cwd, *cwd.parents]:
        venv_python = _venv_python(directory)
        if (directory / "pyproject.toml").exists() and venv_python.exists():
            if has_gobby_pyproject(directory):
                return directory
            break
    return None


def _resolve_install_context(*, verbose: bool = False) -> dict[str, str | bool]:
    """Resolve the execution context for service file generation.

    Returns dict with: python_executable, working_directory, mode,
    home_dir, path_env, log_file, error_log_file, gobby_home, verbose.
    """
    from gobby.config.app import load_config

    config = load_config()

    exe = Path(sys.executable).resolve()
    home_dir = str(Path.home())
    log_file = str(Path(config.telemetry.log_file).expanduser())
    error_log_file = str(Path(config.telemetry.log_file_error).expanduser())

    # Resolve GOBBY_HOME only if explicitly set
    gobby_home = os.environ.get("GOBBY_HOME", "")

    if _is_dev_mode():
        # Dev mode: use the project .venv python, not the global one.
        # First check if sys.executable is already inside the project,
        # otherwise fall back to CWD-based detection.
        project_root = _find_project_root(exe)
        dev_exe = exe

        cwd_project = _find_project_from_cwd()
        if cwd_project and project_root == Path.home():
            # sys.executable is NOT in the project (global install),
            # but CWD IS the project — use the project's .venv python
            project_root = cwd_project
            dev_exe = _venv_python(cwd_project)

        return {
            "python_executable": str(dev_exe),
            "working_directory": str(project_root),
            "mode": "dev",
            "home_dir": home_dir,
            "path_env": _build_path(dev_exe),
            "log_file": log_file,
            "error_log_file": error_log_file,
            "gobby_home": gobby_home,
            "verbose": verbose,
        }

    # Installed mode: working directory is $HOME
    return {
        "python_executable": str(exe),
        "working_directory": home_dir,
        "mode": "installed",
        "home_dir": home_dir,
        "path_env": _build_path(exe),
        "log_file": log_file,
        "error_log_file": error_log_file,
        "gobby_home": gobby_home,
        "verbose": verbose,
    }


def _find_project_root(exe: Path) -> Path:
    """Find the project root directory from the executable path."""
    for parent in exe.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.home()


def _build_path(exe: Path) -> str:
    """Build a PATH that includes the executable's bin directory."""
    sep = os.pathsep
    exe_dir = str(exe.parent)
    if sys.platform == "win32":
        default_path = os.path.expandvars(r"%SystemRoot%\system32;%SystemRoot%")
    else:
        default_path = "/usr/bin:/bin:/usr/sbin:/sbin"
    system_path = os.environ.get("PATH", default_path)
    # Ensure exe dir is first so the right python/gobby is found
    parts = [exe_dir] + [p for p in system_path.split(sep) if p != exe_dir]
    return sep.join(parts)


def _ensure_cli_on_path(project_root: str) -> dict[str, Any]:
    """Ensure the gobby CLI is globally available via ``uv tool install -e``.

    Only needed in dev mode where the CLI isn't installed system-wide.
    Non-fatal — returns a status dict, never raises.
    """
    if shutil.which("gobby"):
        return {"cli_installed": False, "cli_note": "gobby already on PATH"}

    uv = shutil.which("uv")
    if not uv:
        return {"cli_installed": False, "cli_note": "uv not found — install manually"}

    try:
        result = subprocess.run(  # nosec B603 B607 # hardcoded uv command
            [uv, "tool", "install", "-e", project_root],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return {"cli_installed": True, "cli_note": "installed via uv tool install -e"}
        return {
            "cli_installed": False,
            "cli_note": f"uv tool install failed: {result.stderr or result.stdout}",
        }
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"cli_installed": False, "cli_note": f"uv tool install failed: {e}"}


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

    _launchctl_bootout(quiet=False)
    return {"success": True, "platform": "macos"}


def _launchctl_bootout(*, quiet: bool) -> None:
    """Bootout the launchd service (stop + unload)."""
    uid = os.getuid()
    try:
        result = subprocess.run(  # nosec B603 B607
            ["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if not quiet and result.returncode != 0:
            logger.debug(f"launchctl bootout: {result.stderr or result.stdout}")
    except subprocess.TimeoutExpired:
        logger.warning("launchctl bootout timed out")
    except OSError as e:
        if not quiet:
            logger.warning(f"launchctl bootout failed: {e}")


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
    # The plist sets ExitTimeOut=15s, so the process may take that long
    # to terminate.  Poll for up to 16s to avoid racing bootstrap.
    for _ in range(160):  # up to ~16s
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
# Linux (systemd)
# ---------------------------------------------------------------------------


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
