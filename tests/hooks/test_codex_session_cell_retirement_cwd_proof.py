"""Open exec sessions must not poison later literal cwd proofs (#25856).

Codex drives a tty exec session through cells. The launch cell yields; a wait or poll turns
the cell into the session's running envelope; each further poll yields a new cell. A cell
whose script has finished can no longer launch a command, so it must stop counting as a
pending call while its session stays open.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.hooks._path_scope import current_tool_cwd
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookResponse

pytestmark = pytest.mark.unit

PYTEST = "uv run --no-sync pytest tests/hooks/test_codex_session_cell_retirement_cwd_proof.py -q"
RUFF = "uv run --no-sync ruff check src/"
SESSION_ID = 801
STILL_RUNNING = json.dumps({"session_id": SESSION_ID, "output": "still running"})
EXITED = json.dumps({"exit_code": 0, "output": "passed"})
# The pending-cell cap in ExecutionChainCorrelator. Past it, overflow poisons every later proof.
POLLS_PAST_CAP = 70


def _record(payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "response_item", "payload": payload}


def _cell(call_id: str, source: str) -> dict[str, Any]:
    return _record(
        {"type": "custom_tool_call", "name": "exec", "call_id": call_id, "input": source}
    )


def _cell_output(call_id: str, text: str) -> dict[str, Any]:
    return _record({"type": "custom_tool_call_output", "call_id": call_id, "output": text})


def _exited_output(call_id: str) -> dict[str, Any]:
    return _record(
        {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": [{"type": "input_text", "text": EXITED}],
        }
    )


def _yielded(cell_id: str) -> str:
    return f"Script running with cell ID {cell_id}\nWall time 31.2 seconds\nOutput:\n"


def _wait_result(call_id: str, cell_id: str, text: str) -> list[dict[str, Any]]:
    arguments = json.dumps({"cell_id": cell_id, "yield_time_ms": 30000})
    return [
        _record(
            {"type": "function_call", "name": "wait", "call_id": call_id, "arguments": arguments}
        ),
        _record(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": [{"type": "input_text", "text": text}],
            }
        ),
    ]


def _literal(command: str, workdir: Path) -> str:
    return (
        f"text(await tools.exec_command({{cmd:{json.dumps(command)},"
        f"workdir:{json.dumps(str(workdir))},yield_time_ms:1000,max_output_tokens:2000}}));"
    )


def _launch(command: str, workdir: Path) -> str:
    return (
        f"const r = await tools.exec_command({{cmd:{json.dumps(command)},"
        f"workdir:{json.dumps(str(workdir))},tty:true,yield_time_ms:1000}}); text(r);"
    )


def _computed_launch(command: str, workdir: Path) -> str:
    """The same tty launch with the tool reached through a computed name: its command is unreadable."""
    return (
        'const run = tools["exec_" + "command"]; '
        f"const r = await run({{cmd:{json.dumps(command)},"
        f"workdir:{json.dumps(str(workdir))},tty:true,yield_time_ms:1000}}); text(r);"
    )


def _poll() -> str:
    return (
        f"const r = await tools.write_stdin({{session_id:{SESSION_ID},yield_time_ms:1000}}); "
        "text(r);"
    )


def _open_session(launch: str, polls: int, *, waited: bool) -> list[dict[str, Any]]:
    """One tty session, started by a yielding cell and driven by ``polls`` poll cells, still running."""
    records = [_cell("launch", launch), _cell_output("launch", _yielded("cell-0"))]
    records += _wait_result("wait-0", "cell-0", STILL_RUNNING)
    for index in range(polls):
        cell_id = f"cell-{index + 1}"
        records += [
            _cell(f"poll-{index}", _poll()),
            _cell_output(f"poll-{index}", _yielded(cell_id)),
        ]
        if waited:
            records += _wait_result(f"wait-{index + 1}", cell_id, STILL_RUNNING)
    return records


def _hook(
    event_handlers: EventHandlers,
    tmp_path: Path,
    records: list[dict[str, Any]],
    command: str,
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


@pytest.mark.parametrize("polls", [1, POLLS_PAST_CAP])
def test_literal_cell_proves_its_workdir_beside_a_long_open_tty_session(
    event_handlers: EventHandlers, tmp_path: Path, polls: int
) -> None:
    """Each waited poll retires its cell, so an open session never fills the pending set."""
    workdir = tmp_path / "task"
    records = _open_session(_launch(PYTEST, workdir), polls, waited=True)
    records.append(_cell("ruff", _literal(RUFF, workdir)))
    event, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


def test_unwaited_polls_do_not_accumulate_pending_cells(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Polls that yield without a wait each supersede the previous cell of their session."""
    workdir = tmp_path / "task"
    records = _open_session(_launch(PYTEST, workdir), POLLS_PAST_CAP, waited=False)
    records.append(_cell("ruff", _literal(RUFF, workdir)))
    event, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


def test_exited_session_releases_its_unattributed_launch(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Once an unreadable launch's session has exited, nothing it started is still running."""
    workdir = tmp_path / "task"
    records = _open_session(_computed_launch(PYTEST, workdir), 1, waited=True)
    records += [_cell("final", _poll()), _exited_output("final")]
    records.append(_cell("ruff", _literal(RUFF, workdir)))
    event, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


def test_open_unattributed_session_still_blocks_later_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Fail closed: typed input could still reach a session whose command cannot be read."""
    workdir = tmp_path / "task"
    records = _open_session(_computed_launch(PYTEST, workdir), 1, waited=True)
    records.append(_cell("ruff", _literal(RUFF, workdir)))
    _, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "block"
    assert "cannot prove its working directory" in (response.reason or "")
