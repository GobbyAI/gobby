#!/usr/bin/env python3
"""Unified settings validator for all CLI integrations.

Validates hook configuration files across Claude Code, Qwen CLI, Codex,
Grok, and Factory Droid.

CLI is identified via --cli flag (primary) or path-based detection (fallback).

Validates:
- JSON syntax correctness
- Hook structure and dispatcher commands
- All required hook types are configured
- Dispatcher script exists
- CLI-specific requirements (enableHooks, version field, etc.)

Usage:
    validate_settings.py --cli=claude
    validate_settings.py --cli=qwen
    validate_settings.py  # auto-detects from script path

Exit Codes:
    0 - All validations passed
    1 - Validation failed
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# This file is copied to ~/.gobby/hooks and invoked by the system Python, where
# the gobby package may not be importable. Keep its small runtime contracts local.
CLAUDE_PASCAL_HOOK_NAMES: tuple[str, ...] = (
    "SessionStart",
    "InstructionsLoaded",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionDenied",
    "Notification",
    "SubagentStart",
    "SubagentStop",
    "TaskCreated",
    "TaskCompleted",
    "Stop",
    "StopFailure",
    "TeammateIdle",
    "ConfigChange",
    "CwdChanged",
    "FileChanged",
    "WorktreeCreate",
    "WorktreeRemove",
    "PreCompact",
    "PostCompact",
    "SessionEnd",
    "Elicitation",
    "ElicitationResult",
)
DROID_PASCAL_HOOK_NAMES: tuple[str, ...] = (
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "Notification",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "SessionStart",
    "SessionEnd",
)


def is_gobby_hook_command(command: str) -> bool:
    """Return whether a command string belongs to Gobby-managed hooks."""
    return "--gobby-owned" in command


@dataclass(frozen=True)
class ValidationConfig:
    """Per-CLI validation configuration."""

    cli_name: str
    settings_dir: str  # ".claude", ".qwen", etc.
    settings_file: str  # "settings.json" or "hooks.json"
    required_hooks: tuple[str, ...]  # Required hook types
    nested: bool  # True = hooks have nested "hooks" array (Claude/Qwen)
    check_disable_all_hooks: bool = False  # Qwen requires top-level disableAllHooks=false
    check_version: int | None = None  # Reserved for future use
    flat_hooks: bool = False  # Droid: hooks are top-level keys (no "hooks" wrapper)


CLI_VALIDATION_CONFIGS: dict[str, ValidationConfig] = {
    "claude": ValidationConfig(
        cli_name="Claude Code",
        settings_dir=".claude",
        settings_file="settings.json",
        required_hooks=CLAUDE_PASCAL_HOOK_NAMES,
        nested=True,
    ),
    "grok": ValidationConfig(
        cli_name="Grok CLI",
        settings_dir=".grok/hooks",
        settings_file="gobby.json",
        required_hooks=(
            "SessionStart",
            "SessionEnd",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PostToolUseFailure",
            "PreCompact",
            "Stop",
            "Notification",
        ),
        nested=True,
    ),
    "qwen": ValidationConfig(
        cli_name="Qwen CLI",
        settings_dir=".qwen",
        settings_file="settings.json",
        required_hooks=(
            "SessionStart",
            "SessionEnd",
            "UserPromptSubmit",
            "PreToolUse",
            "PermissionRequest",
            "PostToolUse",
            "PostToolUseFailure",
            "Stop",
            "StopFailure",
            "SubagentStart",
            "SubagentStop",
            "PreCompact",
            "PostCompact",
            "Notification",
            "TodoCreated",
            "TodoCompleted",
        ),
        nested=True,
        check_disable_all_hooks=True,
    ),
    "codex": ValidationConfig(
        cli_name="Codex CLI",
        settings_dir=".codex",
        settings_file="hooks.json",
        required_hooks=(
            "PreToolUse",
            "PermissionRequest",
            "PostToolUse",
            "PreCompact",
            "PostCompact",
            "SessionStart",
            "SubagentStart",
            "UserPromptSubmit",
            "SubagentStop",
            "Stop",
        ),
        nested=True,
    ),
    "droid": ValidationConfig(
        cli_name="Factory droid",
        settings_dir=".factory",
        settings_file="hooks.json",
        required_hooks=DROID_PASCAL_HOOK_NAMES,
        nested=True,
        flat_hooks=True,
    ),
}


def detect_cli_config() -> ValidationConfig | None:
    """Detect CLI from --cli flag or script path."""
    parser = argparse.ArgumentParser(description="Gobby Settings Validator")
    parser.add_argument("--cli", default=None, help="CLI name")
    args, _ = parser.parse_known_args()

    if args.cli:
        cli_name = args.cli.lower()
        if cli_name in CLI_VALIDATION_CONFIGS:
            return CLI_VALIDATION_CONFIGS[cli_name]

    # Fallback: detect from script path
    script_path = str(Path(__file__).resolve())
    for cli_name in CLI_VALIDATION_CONFIGS:
        if f".{cli_name}/" in script_path or f"/{cli_name}/" in script_path:
            return CLI_VALIDATION_CONFIGS[cli_name]

    return None


def find_project_root() -> Path:
    """Find project root by walking up from the script's location."""
    # The script lives in <project>/<settings_dir>/hooks/validate_settings.py
    return Path(__file__).parent.parent.parent


def validate(config: ValidationConfig) -> int:
    """Run all validations for a CLI.

    Returns:
        0 if valid, 1 if invalid
    """
    project_root = find_project_root()
    cli_dir = project_root / config.settings_dir
    settings_file = cli_dir / config.settings_file

    # 1. Check settings file exists
    if not settings_file.exists():
        print(f"Settings file not found: {settings_file}")
        return 1

    # 2. Validate JSON syntax
    try:
        with open(settings_file) as f:
            settings = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON syntax: {e}")
        return 1

    print(f"JSON syntax is valid ({config.cli_name})")

    # 3. Check hooks section exists
    if config.flat_hooks:
        # Droid 0.159.1: hook event names are top-level keys (no "hooks" wrapper)
        hooks = settings
    else:
        if "hooks" not in settings:
            print(f"No 'hooks' section found in {config.settings_file}")
            return 1
        hooks = settings["hooks"]
    print("Hooks section found")

    # 4. CLI-specific extra checks
    if config.check_disable_all_hooks:
        if settings.get("disableAllHooks") is not False:
            print(f"disableAllHooks is not set to false (required for {config.cli_name})")
            return 1
        print("disableAllHooks is false")

    if config.check_version is not None:
        version = settings.get("version")
        if version != config.check_version:
            print(f"Expected 'version': {config.check_version}, got: {version}")
            return 1
        print(f"Version field is {config.check_version}")

    # 5. Validate each required hook
    for hook_type in config.required_hooks:
        if hook_type not in hooks:
            print(f"Missing hook type: {hook_type}")
            return 1

        hook_configs = hooks[hook_type]
        if not isinstance(hook_configs, list) or not hook_configs:
            print(f"Invalid hook configuration for: {hook_type}")
            return 1

        if config.nested:
            # Claude/Qwen: nested structure with "hooks" array
            first_config = hook_configs[0]
            if not isinstance(first_config.get("hooks"), list) or not first_config["hooks"]:
                print(f"No 'hooks' array in {hook_type} configuration")
                return 1
            command = first_config["hooks"][0].get("command", "")
        else:
            # Flat structure with "command" directly — preserved for future
            # CLIs (e.g., Codex) that may use non-nested hook configs.
            command = hook_configs[0].get("command", "")

        if not is_gobby_hook_command(command):
            print(f"Warning: {hook_type} not using the gobby-managed hook command")

    print(f"All {len(config.required_hooks)} required hook types configured")

    print(f"\nAll validations passed! ({config.cli_name})")
    return 0


def main() -> int:
    """Main entry point."""
    config = detect_cli_config()
    if config is None:
        print("Could not detect CLI. Use --cli=<name> (claude, grok, qwen, codex, droid)")
        return 1

    return validate(config)


if __name__ == "__main__":
    sys.exit(main())
