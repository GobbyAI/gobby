#!/usr/bin/env python3
"""Validate .claude/settings.json configuration.

This script validates:
- JSON syntax correctness
- Hook structure and dispatcher commands
- All required hook types are configured
- Dispatcher script exists and is executable
"""

import json
import sys
from pathlib import Path


def main() -> int:
    """Validate settings.json configuration.

    Returns:
        0 if valid, 1 if invalid
    """
    # Find settings.json
    claude_dir = Path(__file__).parent.parent
    settings_file = claude_dir / "settings.json"

    if not settings_file.exists():
        print(f"❌ Settings file not found: {settings_file}")
        return 1

    # Validate JSON syntax
    try:
        with open(settings_file) as f:
            settings = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON syntax: {e}")
        return 1

    print("✅ JSON syntax is valid")

    # Check hooks section exists
    if "hooks" not in settings:
        print("❌ No 'hooks' section found in settings")
        return 1

    hooks = settings["hooks"]
    print("✅ Hooks section found")

    # Required hook types
    required_hooks = [
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PreCompact",
        "Notification",
        "Stop",
        "SubagentStart",
        "SubagentStop",
    ]

    # Validate each required hook
    for hook_type in required_hooks:
        if hook_type not in hooks:
            print(f"❌ Missing hook type: {hook_type}")
            return 1

        hook_configs = hooks[hook_type]
        if not isinstance(hook_configs, list) or not hook_configs:
            print(f"❌ Invalid hook configuration for: {hook_type}")
            return 1

        # Check first configuration
        config = hook_configs[0]
        if "hooks" not in config:
            print(f"❌ No 'hooks' array in {hook_type} configuration")
            return 1

        # Check command uses dispatcher
        command = config["hooks"][0].get("command", "")
        if "hook_dispatcher.py" not in command:
            print(f"⚠️  Warning: {hook_type} not using dispatcher pattern")

    print(f"✅ All {len(required_hooks)} required hook types configured")

    # Validate dispatcher exists
    dispatcher = claude_dir / "hooks" / "hook_dispatcher.py"
    if not dispatcher.exists():
        print(f"❌ Dispatcher not found: {dispatcher}")
        return 1

    print("✅ Dispatcher script exists")

    if not dispatcher.stat().st_mode & 0o111:
        print("⚠️  Warning: Dispatcher is not executable")

    print("\n✅ All validations passed!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
