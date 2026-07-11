"""
IDE configuration functions for Gobby installers.

Extracted from shared.py as part of Strangler Fig decomposition (Wave 2).
Handles configuring VS Code-family IDE settings.
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
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

ANTIGRAVITY_IDE_NAMES: frozenset[str] = frozenset({"Antigravity", "Antigravity IDE"})
TERMINAL_TITLE_SETTING = "terminal.integrated.tabs.title"
TERMINAL_TABS_HIDE_CONDITION_SETTING = "terminal.integrated.tabs.hideCondition"
TERMINAL_TITLE_SEQUENCE = "${sequence}"
TERMINAL_TABS_ALWAYS_VISIBLE = "never"
TMUX_PROFILE_NAME = "tmux"


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


def _title_with_sequence(title: Any) -> str:
    if isinstance(title, str):
        stripped = title.strip()
        if TERMINAL_TITLE_SEQUENCE in stripped:
            return stripped
        if stripped:
            return f"{TERMINAL_TITLE_SEQUENCE}${{separator}}{stripped}"

    return TERMINAL_TITLE_SEQUENCE


def _terminal_platform_key() -> str | None:
    """Return the VS Code terminal profile platform key for this host."""
    if sys.platform == "darwin":
        return "osx"
    if sys.platform.startswith("linux"):
        return "linux"
    return None


def _load_ide_settings(settings_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load an IDE settings object, returning a user-facing error on failure."""
    if not settings_path.exists():
        return {}, None
    try:
        with open(settings_path) as settings_file:
            settings = json.load(settings_file)
    except json.JSONDecodeError as exc:
        return None, f"Failed to parse {settings_path}: {exc}"
    except OSError as exc:
        return None, f"Failed to read {settings_path}: {exc}"
    if not isinstance(settings, dict):
        return None, f"Failed to parse {settings_path}: top-level value must be an object"
    return settings, None


def _terminal_integration_updates(
    ide_name: str,
    existing_settings: dict[str, Any],
    platform_key: str,
    tmux_path: str | None,
) -> dict[str, Any]:
    """Build the settings merge needed for full tmux terminal integration."""
    profiles_setting = f"terminal.integrated.profiles.{platform_key}"
    default_profile_setting = f"terminal.integrated.defaultProfile.{platform_key}"
    existing_profiles = existing_settings.get(profiles_setting)
    if existing_profiles is None:
        profiles: dict[str, Any] = {}
    elif isinstance(existing_profiles, dict):
        profiles = existing_profiles
    else:
        raise ValueError(f"{profiles_setting} must be an object")

    updates: dict[str, Any] = {}
    if TMUX_PROFILE_NAME not in profiles:
        if tmux_path is None:
            raise ValueError("tmux executable was not found on PATH")
        updates[profiles_setting] = {
            **profiles,
            TMUX_PROFILE_NAME: {"path": tmux_path, "args": ["new-session"]},
        }

    if existing_settings.get(default_profile_setting) != TMUX_PROFILE_NAME:
        updates[default_profile_setting] = TMUX_PROFILE_NAME

    title = _title_with_sequence(existing_settings.get(TERMINAL_TITLE_SETTING))
    if existing_settings.get(TERMINAL_TITLE_SETTING) != title:
        updates[TERMINAL_TITLE_SETTING] = title

    if ide_name in ANTIGRAVITY_IDE_NAMES:
        current_hide_condition = existing_settings.get(TERMINAL_TABS_HIDE_CONDITION_SETTING)
        if current_hide_condition != TERMINAL_TABS_ALWAYS_VISIBLE:
            updates[TERMINAL_TABS_HIDE_CONDITION_SETTING] = TERMINAL_TABS_ALWAYS_VISIBLE

    return updates


def find_vscode_family_ides_needing_terminal_integration(
    ide_names: tuple[str, ...] = VSCODE_FAMILY_IDE_NAMES,
) -> list[str]:
    """Return installed VS Code-family IDEs whose settings need integration."""
    platform_key = _terminal_platform_key()
    needs_integration: list[str] = []
    for ide_name in ide_names:
        config_dir = _get_ide_config_dir(ide_name)
        if not config_dir.exists():
            continue
        if platform_key is None:
            needs_integration.append(ide_name)
            continue

        settings, error = _load_ide_settings(config_dir / "User" / "settings.json")
        if error is not None or settings is None:
            needs_integration.append(ide_name)
            continue
        try:
            if _terminal_integration_updates(ide_name, settings, platform_key, "tmux"):
                needs_integration.append(ide_name)
        except ValueError:
            needs_integration.append(ide_name)
    return needs_integration


def configure_ide_terminal_integration(ide_name: str) -> dict[str, Any]:
    """Configure full tmux terminal integration for a VS Code-family IDE.

    Preserves custom profiles, selects tmux as the default for new terminals,
    and adds ``${sequence}`` title passthrough. Uses backup + atomic replace.

    Skips silently if the IDE is not installed (config dir doesn't exist).

    Args:
        ide_name: IDE name matching the Application Support / config dir.

    Returns:
        Dict with 'success', 'added', 'updated', 'already_configured', 'skipped',
        'backup_path', and 'error' keys.
    """
    result: dict[str, Any] = {
        "success": False,
        "added": False,
        "updated": False,
        "already_configured": False,
        "skipped": False,
        "backup_path": None,
        "error": None,
        "warning": None,
    }

    config_dir = _get_ide_config_dir(ide_name)
    if not config_dir.exists():
        # IDE not installed — skip silently
        result["success"] = True
        result["skipped"] = True
        return result

    settings_path = config_dir / "User" / "settings.json"

    platform_key = _terminal_platform_key()
    if platform_key is None:
        result.update(
            success=True,
            skipped=True,
            warning=f"unsupported platform {sys.platform!r}",
        )
        return result

    existing_settings, error = _load_ide_settings(settings_path)
    if error is not None or existing_settings is None:
        result["error"] = error
        return result

    profiles_setting = f"terminal.integrated.profiles.{platform_key}"
    existing_profiles = existing_settings.get(profiles_setting)
    has_tmux_profile = (
        isinstance(existing_profiles, dict) and TMUX_PROFILE_NAME in existing_profiles
    )
    tmux_path = None if has_tmux_profile else shutil.which("tmux")
    if not has_tmux_profile and tmux_path is None:
        result.update(
            success=True,
            skipped=True,
            warning="tmux executable was not found on PATH",
        )
        return result

    try:
        updates = _terminal_integration_updates(
            ide_name, existing_settings, platform_key, tmux_path
        )
    except ValueError as exc:
        result["error"] = f"Failed to configure {settings_path}: {exc}"
        return result

    if not updates:
        result["success"] = True
        result["already_configured"] = True
        return result

    # Create backup if file exists
    if settings_path.exists():
        timestamp = int(time.time())
        backup_path = settings_path.parent / f"settings.json.{timestamp}.backup"
        try:
            shutil.copy2(settings_path, backup_path)
            result["backup_path"] = str(backup_path)
        except OSError as e:
            result["error"] = f"Failed to create backup: {e}"
            return result

    result["added"] = any(key not in existing_settings for key in updates)
    result["updated"] = any(
        key in existing_settings and existing_settings.get(key) != value
        for key, value in updates.items()
    )
    merged_settings = {**existing_settings, **updates}

    temp_path: Path | None = None
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temp_name = tempfile.mkstemp(
            dir=settings_path.parent,
            prefix=".settings.json.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        with os.fdopen(file_descriptor, "w") as settings_file:
            json.dump(merged_settings, settings_file, indent=2)
            settings_file.write("\n")
            settings_file.flush()
            os.fsync(settings_file.fileno())
        os.replace(temp_path, settings_path)
        temp_path = None
    except OSError as e:
        result["error"] = f"Failed to write {settings_path}: {e}"
        return result
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    result["success"] = True
    return result


def configure_vscode_family_terminal_integration(
    ide_names: tuple[str, ...] = VSCODE_FAMILY_IDE_NAMES,
) -> dict[str, dict[str, Any]]:
    """Configure full tmux integration for known VS Code-family IDEs."""
    return {ide_name: configure_ide_terminal_integration(ide_name) for ide_name in ide_names}
