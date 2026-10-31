"""Cells that cannot reach a shell, and cells orphaned by compaction, must not block cwd proof."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.codex_impl.execution_chain import ExecutionChainCorrelator
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.hooks._path_scope import current_tool_cwd
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookResponse

pytestmark = pytest.mark.unit

PYTEST = "uv run --no-sync pytest tests/hooks/test_codex_orphan_cell_cwd_proof.py -q"

# Synthetic stand-in for an MCP-only cell. Its text mentions exec_command, which is inert
# string content and must not make the cell look shell-capable.
MCP_CELL = (
    'text(await tools.mcp_call({server_name:"gobby-sessions",tool_name:"set_handoff",'
    'arguments:{clear_session:false,current_state:"Next: check exec_command workdirs"}}));'
)
PLAIN_MCP_CELL = 'text(await tools.mcp_call({server_name:"gobby-tasks",tool_name:"get_task"}));'


def _literal(command: str, workdir: Path | str) -> str:
    return (
        f"text(await tools.exec_command({{cmd:{json.dumps(command)},"
        f"workdir:{json.dumps(str(workdir))},yield_time_ms:1000,max_output_tokens:1800}}));"
    )


# Cells that can start a shell whose command this hook cannot read.
UNKNOWN_CELLS = {
    "two_calls": _literal(PYTEST, "/elsewhere") * 2,
    "computed": (
        'const run = tools["exec_" + "command"];'
        ' text(await run({cmd:"uv run --no-sync pytest", workdir:"/elsewhere"}));'
    ),
    "eval": """text(await eval('tools.exec_command({cmd:"pytest"})'));""",
    "aliased": (
        "const { exec_command: run } = tools;"
        ' text(await run({cmd:"uv run --no-sync pytest", workdir:"/elsewhere"}));'
    ),
    # A regex literal's quote makes the rest of the line look like a string to a lexer
    # that does not read regex syntax, so the shell call would be blanked as text.
    "regex_quote": 'const r = /"/; tools.exec_command({cmd: command, workdir: cwd}); // "',
    "regex_after_keyword": "void /'/; tools.exec_command({cmd: command, workdir: cwd}); // '",
}


def _call(call_id: str, source: str) -> dict[str, Any]:
    return {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": call_id,
            "input": source,
        },
    }


def _output(call_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": [{"type": "input_text", "text": text}],
        },
    }


def _compacted() -> dict[str, Any]:
    """Codex's durable compact boundary: the context is replaced and open calls lose output."""
    return {"type": "compacted", "timestamp": "2026-10-30T01:40:00.000Z"}


def _hook(
    event_handlers: EventHandlers,
    tmp_path: Path,
    records: list[dict[str, Any]],
    command: str = PYTEST,
) -> tuple[HookEvent, HookResponse]:
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    event = CodexHooksAdapter().translate_to_hook_event(
        {
            "hook_type": "PreToolUse",
            "input_data": {
                "tool_name": "Bash",
                "tool_use_id": "exec-inner",
                "tool_input": {"command": command},
                "cwd": str(tmp_path / "launcher"),
                "transcript_path": str(transcript),
            },
        }
    )
    assert event is not None
    return event, event_handlers.handle_before_tool(event)


@pytest.mark.parametrize("cell", [MCP_CELL, PLAIN_MCP_CELL])
def test_unattributed_cell_without_shell_reach_is_not_pending(cell: str) -> None:
    correlator = ExecutionChainCorrelator()
    assert (
        correlator.register_call("cell", "functions.exec", cell, allow_unattributed=True) is False
    )
    assert correlator.snapshot_state()["calls"] == {}


@pytest.mark.parametrize("name", list(UNKNOWN_CELLS))
def test_unattributed_shell_capable_cell_stays_pending(name: str) -> None:
    correlator = ExecutionChainCorrelator()
    registered = correlator.register_call(
        "cell", "functions.exec", UNKNOWN_CELLS[name], allow_unattributed=True
    )
    assert registered
    assert correlator.snapshot_state()["calls"]["cell"]["literal_command"] is None


@pytest.mark.parametrize("cell", [MCP_CELL, PLAIN_MCP_CELL])
def test_pending_mcp_cell_does_not_block_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path, cell: str
) -> None:
    workdir = tmp_path / "task"
    records = [_call("handoff", cell), _call("pytest", _literal(PYTEST, workdir))]
    event, response = _hook(event_handlers, tmp_path, records)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


@pytest.mark.parametrize("cell", [MCP_CELL, *UNKNOWN_CELLS.values()])
def test_cell_orphaned_by_compaction_does_not_block_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path, cell: str
) -> None:
    """The 2026-10-30 shape: a set_handoff cell loses its output to compaction, then pytest runs."""
    workdir = tmp_path / "task"
    records = [_call("d28f5a5e", cell), _compacted(), _call("pytest", _literal(PYTEST, workdir))]
    event, response = _hook(event_handlers, tmp_path, records)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


@pytest.mark.parametrize("name", list(UNKNOWN_CELLS))
def test_shell_capable_cell_without_compaction_still_fails_closed(
    event_handlers: EventHandlers, tmp_path: Path, name: str
) -> None:
    workdir = tmp_path / "task"
    records = [_call("unknown", UNKNOWN_CELLS[name]), _call("pytest", _literal(PYTEST, workdir))]
    event, response = _hook(event_handlers, tmp_path, records)
    assert response.decision == "block", current_tool_cwd(event.data)
    assert "cannot prove its working directory" in (response.reason or "")
    assert "one exec_command per functions.exec cell" in (response.reason or "")


def test_yielded_unknown_cell_survives_compaction_and_still_fails_closed(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """A yielded cell has output and may still be running a shell, so compaction keeps it."""
    workdir = tmp_path / "task"
    records = [
        _call("unknown", UNKNOWN_CELLS["two_calls"]),
        _output("unknown", "Script running with cell ID cell-1"),
        _compacted(),
        _call("pytest", _literal(PYTEST, workdir)),
    ]
    event, response = _hook(event_handlers, tmp_path, records)
    assert response.decision == "block", current_tool_cwd(event.data)
    assert "cannot prove its working directory" in (response.reason or "")
