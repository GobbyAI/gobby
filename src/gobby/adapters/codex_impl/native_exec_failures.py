"""Read authoritative terminal wrapper failures without deriving shell outcomes."""

import json
import re
from pathlib import Path

from gobby.adapters.codex_impl.cell_shell_reach import static_code

_FAILED_HEADER = re.compile(r"Script failed\nWall time \d+(?:\.\d+)? seconds\nOutput:\n")
_SIMPLE_CELL = re.compile(
    r"\s*text\(\s*await\s+tools\.exec_command\(\s*\{[^{}()]*\}\s*\)\s*\)\s*;?\s*"
)


def is_simple_native_exec_cell(arguments: object) -> bool:
    """Forbid JavaScript that can throw after a successful background launch.

    Literal argument validation belongs to the caller. This additional shape proof
    permits only a direct awaited result print.
    """
    if not isinstance(arguments, str) or (code := static_code(arguments)) is None:
        return False
    return _SIMPLE_CELL.fullmatch(code) is not None


def failed_native_outer_calls(transcript_path: str, candidates: set[str]) -> frozenset[str]:
    """Require one complete provider failure envelope and no earlier yield/process receipt.

    Only the provider's header is evidence. Printed output, partial records, repeated
    outputs and cells that exposed any result before throwing cannot prove abandonment.
    """
    seen: set[str] = set()
    failed: set[str] = set()
    repeated: set[str] = set()
    try:
        with Path(transcript_path).open(encoding="utf-8") as transcript:
            for line in transcript:
                if not line.endswith("\n"):
                    return frozenset()
                row = json.loads(line)
                if not isinstance(row, dict) or row.get("type") != "response_item":
                    continue
                payload = row.get("payload")
                if (
                    not isinstance(payload, dict)
                    or payload.get("type") != "custom_tool_call_output"
                ):
                    continue
                call_id = payload.get("call_id")
                if not isinstance(call_id, str) or call_id not in candidates:
                    continue
                if call_id in seen:
                    repeated.add(call_id)
                seen.add(call_id)
                output = payload.get("output")
                if not isinstance(output, list) or len(output) != 2:
                    continue
                header, error = output
                if not all(isinstance(block, dict) for block in output):
                    continue
                if header.get("type") != "input_text" or error.get("type") != "input_text":
                    continue
                text, detail = header.get("text"), error.get("text")
                if (
                    isinstance(text, str)
                    and _FAILED_HEADER.fullmatch(text)
                    and isinstance(detail, str)
                    and detail.startswith("Script error:\nexec_command failed: CreateProcess")
                ):
                    failed.add(call_id)
    except (OSError, UnicodeError, ValueError):
        return frozenset()
    return frozenset(failed - repeated)
