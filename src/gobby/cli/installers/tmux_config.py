"""Install tmux settings needed for reliable macOS clipboard copies."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

_BEGIN_MARKER = "# BEGIN GOBBY MANAGED TMUX CLIPBOARD"
_END_MARKER = "# END GOBBY MANAGED TMUX CLIPBOARD"
_MANAGED_BLOCK = f"""{_BEGIN_MARKER}
# Copy through the macOS pasteboard to preserve UTF-8 in IDE terminals.
set-option -s set-clipboard off
set-option -s copy-command 'pbcopy'
{_END_MARKER}
"""
_MANAGED_BLOCK_PATTERN = re.compile(
    rf"(?ms)^{re.escape(_BEGIN_MARKER)}\n.*?^{re.escape(_END_MARKER)}(?:\n|$)"
)


def _tmux_config_path(home: Path) -> Path:
    """Return the existing user config tmux will load, preferring the legacy path."""
    legacy_path = home / ".tmux.conf"
    if legacy_path.exists():
        return legacy_path

    xdg_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    xdg_path = xdg_home / "tmux" / "tmux.conf"
    if xdg_path.exists():
        return xdg_path
    return legacy_path


def _merge_managed_block(existing: str) -> str:
    """Add or refresh Gobby's managed block without changing surrounding config."""
    match = _MANAGED_BLOCK_PATTERN.search(existing)
    if match is not None:
        return f"{existing[: match.start()]}{_MANAGED_BLOCK}{existing[match.end() :]}"

    if not existing:
        return _MANAGED_BLOCK
    separator = "\n" if existing.endswith("\n") else "\n\n"
    return f"{existing}{separator}{_MANAGED_BLOCK}"


def _write_atomic(path: Path, content: str) -> None:
    """Atomically replace a config file while retaining its existing mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    file_descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.chmod(mode)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _apply_to_running_server(tmux_path: str) -> bool:
    """Apply clipboard options to an existing tmux server when one is running."""
    commands = (
        [tmux_path, "set-option", "-s", "set-clipboard", "off"],
        [tmux_path, "set-option", "-s", "copy-command", "pbcopy"],
    )
    try:
        results = [
            subprocess.run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for command in commands
        ]
    except OSError:
        return False
    return all(result.returncode == 0 for result in results)


def configure_tmux_clipboard(*, home: Path | None = None) -> dict[str, object]:
    """Configure macOS tmux copies to bypass terminal OSC 52 transcoding.

    The managed block is safe to refresh on upgrades. Existing user content and
    file permissions are preserved. A running tmux server is updated immediately;
    the config file remains the source of truth for future servers.
    """
    result: dict[str, object] = {
        "success": True,
        "skipped": False,
        "updated": False,
        "live_applied": False,
        "config_path": None,
        "error": None,
    }
    if sys.platform != "darwin":
        result["skipped"] = True
        return result

    tmux_path = shutil.which("tmux")
    pbcopy_path = shutil.which("pbcopy")
    if tmux_path is None or pbcopy_path is None:
        result["skipped"] = True
        return result

    logical_config_path = _tmux_config_path(home or Path.home())
    config_path = (
        logical_config_path.resolve() if logical_config_path.is_symlink() else logical_config_path
    )
    result["config_path"] = str(config_path)
    try:
        existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    except UnicodeDecodeError:
        result["success"] = False
        result["error"] = f"Refusing to modify non-UTF-8 tmux config: {config_path}"
        return result
    except OSError as exc:
        result["success"] = False
        result["error"] = f"Failed to read tmux config {config_path}: {exc}"
        return result

    updated = _merge_managed_block(existing)
    if updated != existing:
        try:
            _write_atomic(config_path, updated)
        except OSError as exc:
            result["success"] = False
            result["error"] = f"Failed to write tmux config {config_path}: {exc}"
            return result
        result["updated"] = True

    result["live_applied"] = _apply_to_running_server(tmux_path)
    return result
