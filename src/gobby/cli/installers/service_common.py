"""
Shared helpers for OS-level service installation.

This module owns template rendering and install-context resolution used by
the launchd, systemd, and Windows service backends.
"""

import os
import shutil
import subprocess  # nosec B404 # subprocess needed for installer setup commands
import sys
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

# Template directory
_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "install" / "shared" / "services"
_XML_TEMPLATE_SUFFIXES = (".plist.j2", ".xml.j2")
_TEMPLATE_OPTIONS: dict[str, Any] = {
    "loader": FileSystemLoader(str(_TEMPLATES_DIR)),
    "trim_blocks": True,
    "lstrip_blocks": True,
    "keep_trailing_newline": True,
}
_TEXT_TEMPLATE_ENV = Environment(autoescape=False, **_TEMPLATE_OPTIONS)
_XML_TEMPLATE_ENV = Environment(autoescape=True, **_TEMPLATE_OPTIONS)

# Service identifiers
LAUNCHD_LABEL = "com.gobby.daemon"
LAUNCHD_PLIST_NAME = f"{LAUNCHD_LABEL}.plist"
SYSTEMD_UNIT_NAME = "gobby-daemon.service"


def _render_template(template_name: str, **context: Any) -> str:
    """Render a Jinja2 template from the services directory."""
    env = (
        _XML_TEMPLATE_ENV if template_name.endswith(_XML_TEMPLATE_SUFFIXES) else _TEXT_TEMPLATE_ENV
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
