"""#22103 probe 3, UI automation: which terminal and desktop channels any rule covers.

Static. Reads bundled rule templates and the operator-tool constant; sends no
keys, captures no output, launches nothing, calls no daemon. Managed SRT
AppleEvents and Unix-socket settings come from probe_credential_paths.py.
Run: uv run python <this file>
"""

from pathlib import Path

import yaml

from gobby.workflows.enforcement.blocking import OPERATOR_TOOLS

print(f"OPERATOR_TOOLS={sorted(OPERATOR_TOOLS)}")

rules_root = Path("src/gobby/install/shared/workflows/rules")
for rule_file in sorted(rules_root.rglob("*.yaml")):
    text = rule_file.read_text()
    for name, rule in (yaml.safe_load(text).get("rules") or {}).items():
        effects = rule.get("effects") or []
        mcp_tools = sorted({tool for effect in effects for tool in effect.get("mcp_tools") or []})
        if any(tool.endswith((":send_keys", ":capture_output")) for tool in mcp_tools):
            print(f"rule {name}: mcp_tools={mcp_tools} when={' '.join(rule['when'].split())!r}")

needles = ("capture_output", "tmux send-keys", "osascript", "claude-in-chrome", "computer-use")
for needle in needles:
    hits = [
        str(path.relative_to(rules_root))
        for path in sorted(rules_root.rglob("*.yaml"))
        if needle in path.read_text()
    ]
    print(f"rule templates naming {needle!r}: {hits or 'none'}")
