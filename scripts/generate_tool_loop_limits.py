"""Generate Python tool-loop contract bindings from gcore's versioned artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "crates/gcore/contracts/tool_loop_limits.v1.json"
OUTPUT_PATH = ROOT / "src/gobby/_generated_tool_loop_limits.py"


def render_binding(contract: dict[str, Any]) -> str:
    """Render the complete Python binding for one contract artifact."""
    fields = contract["fields"]
    return f'''"""Generated from ``crates/gcore/contracts/tool_loop_limits.v1.json``.

Do not edit by hand; run ``uv run python scripts/generate_tool_loop_limits.py``.
"""

from __future__ import annotations

from typing import TypedDict

TOOL_LOOP_CONTRACT = {json.dumps(contract["contract"])}
TOOL_LOOP_CONTRACT_VERSION = {contract["version"]}
TOOL_LOOP_CONFIG_PREFIX = {json.dumps(contract["config_prefix"])}

DEFAULT_MAX_TURNS: int | None = {fields["max_turns"]["default"]!r}
DEFAULT_MAX_TOOL_CALLS = {fields["max_tool_calls"]["default"]}
DEFAULT_MAX_BYTES_PER_TOOL_RESULT = {fields["max_bytes_per_tool_result"]["default"]}
DEFAULT_TOOL_TIMEOUT_SECONDS = {fields["tool_timeout_seconds"]["default"]}
DEFAULT_LOOP_TIMEOUT_SECONDS = {fields["loop_timeout_seconds"]["default"]}


class ToolLoopLimitsDict(TypedDict):
    """Complete version-1 wire representation."""

    max_turns: int | None
    max_tool_calls: int
    max_bytes_per_tool_result: int
    tool_timeout_seconds: float
    loop_timeout_seconds: int
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the checked-in binding differs from the contract",
    )
    args = parser.parse_args()

    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    expected = render_binding(contract)
    if args.check:
        if OUTPUT_PATH.read_text(encoding="utf-8") != expected:
            parser.error(
                "generated binding is stale; run "
                "`uv run python scripts/generate_tool_loop_limits.py`"
            )
        return 0

    OUTPUT_PATH.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
