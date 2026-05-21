"""
IDE configuration functions for Gobby installers.

Extracted from shared.py as part of Strangler Fig decomposition (Wave 2).
Handles configuring VS Code-family IDE settings.
"""

import json
import os
import sys
import time
from pathlib import Path
from shutil import copy2
from typing import Any

VSCODE_FAMILY_IDE_NAMES: tuple[str, ...] = (
    "Code",
    "Code - Insiders",
    "Cursor",
    "Windsurf",
    "VSCodium",
    "Code - OSS",
    "Antigravity",
    "Antigravity IDE",
)


def _get_ide_config_dir(ide_name: str) -> Path:
    """Get the IDE's config root directory (cross-platform).

    macOS:   ~/Library/Application Support/<ide_name>/
    Linux:   ~/.config/<ide_name>/
    Windows: %APPDATA%/<ide_name>/
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / ide_name
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / ide_name
    else:
        return Path.home() / ".config" / ide_name


def configure_ide_terminal_title(ide_name: str) -> dict[str, Any]:
    """Configure terminal.integrated.tabs.title for a VS Code-family IDE.

    Adds ``${sequence}`` so tmux ``set-titles`` OSC escapes propagate to
    tab/sidebar labels. Uses backup + atomic write pattern. No-op if
    already configured.

    Skips silently if the IDE is not installed (config dir doesn't exist).

    Args:
        ide_name: IDE name matching the Application Support / config dir.

    Returns:
        Dict with 'success', 'added', 'already_configured', 'skipped',
        'backup_path', and 'error' keys.
    """
    result: dict[str, Any] = {
        "success": False,
        "added": False,
        "already_configured": False,
        "skipped": False,
        "backup_path": None,
        "error": None,
    }

    config_dir = _get_ide_config_dir(ide_name)
    if not config_dir.exists():
        # IDE not installed — skip silently
        result["success"] = True
        result["skipped"] = True
        return result

    settings_path = config_dir / "User" / "settings.json"

    # Load existing settings or start with empty dict
    existing_settings: dict[str, Any] = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                existing_settings = json.load(f)
        except json.JSONDecodeError as e:
            result["error"] = f"Failed to parse {settings_path}: {e}"
            return result
        except OSError as e:
            result["error"] = f"Failed to read {settings_path}: {e}"
            return result

    # Check if already configured
    setting_key = "terminal.integrated.tabs.title"
    if setting_key in existing_settings:
        result["success"] = True
        result["already_configured"] = True
        return result

    # Create backup if file exists
    if settings_path.exists():
        timestamp = int(time.time())
        backup_path = settings_path.parent / f"settings.json.{timestamp}.backup"
        try:
            copy2(settings_path, backup_path)
            result["backup_path"] = str(backup_path)
        except OSError as e:
            result["error"] = f"Failed to create backup: {e}"
            return result

    # Add the setting
    existing_settings[setting_key] = "${sequence}"

    # Ensure User/ directory exists
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Write updated settings
    try:
        with open(settings_path, "w") as f:
            json.dump(existing_settings, f, indent=2)
    except OSError as e:
        result["error"] = f"Failed to write {settings_path}: {e}"
        return result

    result["success"] = True
    result["added"] = True
    return result


def configure_vscode_family_terminal_titles(
    ide_names: tuple[str, ...] = VSCODE_FAMILY_IDE_NAMES,
) -> dict[str, dict[str, Any]]:
    """Configure tmux title passthrough for known VS Code-family IDEs."""
    return {ide_name: configure_ide_terminal_title(ide_name) for ide_name in ide_names}
