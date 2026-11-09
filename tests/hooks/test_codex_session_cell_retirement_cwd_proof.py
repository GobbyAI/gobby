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
EXITED = json.dumps({"exit_code": 0, "output": "passed"})
# The pending-cell cap in ExecutionChainCorrelator. Past it, overflow poisons every later proof.
POLLS_PAST_CAP = 70
# The pending-session cap: the session after the last one evicts the oldest open session.
SESSION_CAP = 64


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


def _still_running(session_id: int) -> str:
    return json.dumps({"session_id": session_id, "output": "still running"})


def _poll(session_id: int = SESSION_ID) -> str:
    return (
        f"const r = await tools.write_stdin({{session_id:{session_id},yield_time_ms:1000}}); "
        "text(r);"
    )


def _typed_poll(session_id: int, chars: str) -> str:
    return (
        f"const r = await tools.write_stdin({{session_id:{session_id},chars:{json.dumps(chars)},"
        "yield_time_ms:1000}); text(r);"
    )


def _open_session(
    launch: str,
    polls: int,
    *,
    waited: bool,
    session_id: int = SESSION_ID,
    tag: str = "",
) -> list[dict[str, Any]]:
    """One tty session, started by a yielding cell and driven by ``polls`` poll cells, still running.

    ``tag`` prefixes every call and cell id so several sessions can share one rollout.
    """
    still_running = _still_running(session_id)
    records = [
        _cell(f"{tag}launch", launch),
        _cell_output(f"{tag}launch", _yielded(f"{tag}cell-0")),
    ]
    records += _wait_result(f"{tag}wait-0", f"{tag}cell-0", still_running)
    for index in range(polls):
        cell_id = f"{tag}cell-{index + 1}"
        records += [
            _cell(f"{tag}poll-{index}", _poll(session_id)),
            _cell_output(f"{tag}poll-{index}", _yielded(cell_id)),
        ]
        if waited:
            records += _wait_result(f"{tag}wait-{index + 1}", cell_id, still_running)
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


def test_finished_long_session_does_not_poison_later_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Overflow latches for the whole rollout, so a finished long session must not leave it set."""
    workdir = tmp_path / "task"
    records = _open_session(_launch(PYTEST, workdir), POLLS_PAST_CAP, waited=True)
    records += [_cell("final", _poll()), _exited_output("final")]
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


def test_evicted_unattributed_session_still_blocks_later_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Evicting the oldest session must not drop a live unattributed session from the proof."""
    workdir = tmp_path / "task"
    records = _open_session(_computed_launch(PYTEST, workdir), 0, waited=True)
    for number in range(SESSION_CAP):
        records += _open_session(
            _launch(PYTEST, workdir), 0, waited=True, session_id=900 + number, tag=f"s{number}-"
        )
    records.append(_cell("ruff", _literal(RUFF, workdir)))
    _, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "block"
    assert "cannot prove its working directory" in (response.reason or "")


def test_colliding_session_id_does_not_drop_a_live_unattributed_session(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """A second session on the same id must not drop the live unattributed session."""
    workdir = tmp_path / "task"
    records = _open_session(_computed_launch(PYTEST, workdir), 0, waited=True)
    records += _open_session(_launch(PYTEST, workdir), 0, waited=True, tag="s0-")
    records.append(_cell("ruff", _literal(RUFF, workdir)))
    _, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "block"
    assert "cannot prove its working directory" in (response.reason or "")


def test_session_already_ambiguous_does_not_admit_a_later_unattributed_launch(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """A launch reporting an already ambiguous session id is dropped; the proof must stay closed."""
    workdir = tmp_path / "task"
    records = _open_session(_computed_launch(PYTEST, workdir), 0, waited=True)
    records += _open_session(_computed_launch(PYTEST, workdir), 0, waited=True, tag="s0-")
    records += _open_session(_computed_launch(PYTEST, workdir), 0, waited=True, tag="s1-")
    records.append(_cell("ruff", _literal(RUFF, workdir)))
    _, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "block"
    assert "cannot prove its working directory" in (response.reason or "")


def test_attributed_session_collision_keeps_later_literal_cwd_proof_open(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Attributed sessions name their commands, so an id collision between them must not block."""
    workdir = tmp_path / "task"
    records = _open_session(_launch(PYTEST, workdir), 0, waited=True)
    records += _open_session(_launch(PYTEST, workdir), 0, waited=True, tag="s0-")
    records.append(_cell("ruff", _literal(RUFF, workdir)))
    event, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


def test_unattributed_launch_colliding_with_attributed_session_blocks_proof(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """The collision drops an unattributed launch, so the proof must stay closed."""
    workdir = tmp_path / "task"
    records = _open_session(_launch(PYTEST, workdir), 0, waited=True)
    records += _open_session(_computed_launch(PYTEST, workdir), 0, waited=True, tag="s0-")
    records.append(_cell("ruff", _literal(RUFF, workdir)))
    _, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "block"
    assert "cannot prove its working directory" in (response.reason or "")


def test_typed_input_does_not_relabel_an_unattributed_session(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Typed input names no launch: a session whose command is unreadable stays unattributed."""
    workdir = tmp_path / "task"
    records = _open_session(_computed_launch(PYTEST, workdir), 0, waited=True)
    records += [
        _cell("typed", _typed_poll(SESSION_ID, "echo hi\n")),
        _cell_output("typed", _yielded("typed-cell")),
    ]
    records += _wait_result("typed-wait", "typed-cell", _still_running(SESSION_ID))
    records.append(_cell("ruff", _literal(RUFF, workdir)))
    _, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "block"
    assert "cannot prove its working directory" in (response.reason or "")


def test_direct_typed_input_does_not_relabel_an_unattributed_session(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """A direct write_stdin is typed input too, so it must not name an unreadable launch."""
    workdir = tmp_path / "task"
    records = _open_session(_computed_launch(PYTEST, workdir), 0, waited=True)
    typed_arguments = json.dumps(
        {"session_id": SESSION_ID, "chars": "echo hi\n", "yield_time_ms": 1000}
    )
    records += [
        _record(
            {
                "type": "function_call",
                "name": "write_stdin",
                "call_id": "typed",
                "arguments": typed_arguments,
            }
        ),
        _record(
            {
                "type": "function_call_output",
                "call_id": "typed",
                "output": [{"type": "input_text", "text": _still_running(SESSION_ID)}],
            }
        ),
    ]
    records.append(_cell("ruff", _literal(RUFF, workdir)))
    _, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "block"
    assert "cannot prove its working directory" in (response.reason or "")


def test_attributed_session_eviction_keeps_later_literal_cwd_proof_open(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """An evicted attributed session names its own command, so it must not block the proof."""
    workdir = tmp_path / "task"
    records: list[dict[str, Any]] = []
    for number in range(SESSION_CAP + 1):
        records += _open_session(
            _launch(PYTEST, workdir), 0, waited=True, session_id=900 + number, tag=f"s{number}-"
        )
    records.append(_cell("ruff", _literal(RUFF, workdir)))
    event, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


def _typed_into_dead_session(session_id: int) -> str:
    return (
        f'text(await tools.write_stdin({{session_id:{session_id},chars:"ls\\n",'
        "yield_time_ms:1000,max_output_tokens:5000}));"
    )


def _failed_write_output(call_id: str, session_id: int) -> dict[str, Any]:
    """The structured failure a write_stdin cell returns when its session has already exited."""
    return _record(
        {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "output": [
                {"type": "input_text", "text": "Script failed\nWall time 0.0 seconds\nOutput:\n"},
                {
                    "type": "input_text",
                    "text": f"Script error:\nwrite_stdin failed: Unknown process id {session_id}",
                },
            ],
        }
    )


def test_failed_typed_write_does_not_latch_a_later_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Typed input that failed never reached a shell, so it must not refuse a literal proof."""
    workdir = tmp_path / "task"
    records = [
        _cell("launch", _launch(PYTEST, workdir)),
        _cell_output("launch", _yielded("cell-y")),
        _cell("typed", _typed_into_dead_session(SESSION_ID)),
        _failed_write_output("typed", SESSION_ID),
        _cell("ruff", _literal(RUFF, workdir)),
    ]
    event, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


def test_typed_write_that_may_have_reached_a_shell_still_latches(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Every other typed write to an unmapped session keeps the latch: the write may have run."""
    workdir = tmp_path / "task"
    records = [
        _cell("launch", _launch(PYTEST, workdir)),
        _cell_output("launch", _yielded("cell-y")),
        _cell("typed", _typed_into_dead_session(SESSION_ID)),
        _cell_output("typed", "Output:\nls\n"),
        _cell("ruff", _literal(RUFF, workdir)),
    ]
    _, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "block"
    assert "cannot prove its working directory" in (response.reason or "")


# A computed chars value inside a template literal can type anything: it keeps its latch.
COMPUTED_TYPED_INPUT = (
    'const c = `l${"s"}\\n`; text(await tools.write_stdin({session_id:801,chars:c}));'
)


def test_computed_typed_input_beside_a_pending_yielded_cell_still_latches(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """A computed chars value may type into a shell no execution owns: the latch holds."""
    workdir = tmp_path / "task"
    records = [
        _cell("launch", _launch(PYTEST, workdir)),
        _cell_output("launch", _yielded("cell-y")),
        _cell("typed", COMPUTED_TYPED_INPUT),
        _cell_output("typed", "Script completed\nWall time 0.0 seconds\nOutput:\n"),
        _cell("ruff", _literal(RUFF, workdir)),
    ]
    _, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "block"
    assert "cannot prove its working directory" in (response.reason or "")


def _task_complete() -> dict[str, Any]:
    return {
        "type": "event_msg",
        "timestamp": "2026-10-30T00:39:39.000Z",
        "payload": {"type": "task_complete"},
    }


def _compacted() -> dict[str, Any]:
    return {"type": "compacted", "timestamp": "2026-10-30T01:40:00.000Z"}


@pytest.mark.parametrize("polls", [1, 3])
def test_unwaited_yielded_cell_from_a_finished_turn_does_not_refuse_a_later_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path, polls: int
) -> None:
    """The probe shape: a computed launch yields, empty polls keep it running, then the turn ends and compacts."""
    workdir = tmp_path / "task"
    records = [
        _cell("launch", _computed_launch(PYTEST, workdir)),
        _cell_output("launch", _yielded("cell-y")),
    ]
    for index in range(polls):
        records += _wait_result(f"poll-{index}", "cell-y", _yielded("cell-y"))
    records += [_task_complete(), _compacted(), _cell("ruff", _literal(RUFF, workdir))]
    event, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


def test_unwaited_yielded_cell_in_the_current_turn_still_refuses_a_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Fail closed: without a turn boundary after the yield, the cell may still be live."""
    workdir = tmp_path / "task"
    records = [
        _cell("launch", _computed_launch(PYTEST, workdir)),
        _cell_output("launch", _yielded("cell-y")),
        _cell("ruff", _literal(RUFF, workdir)),
    ]
    _, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "block"
    assert "cannot prove its working directory" in (response.reason or "")


def test_task_complete_without_compaction_still_refuses_a_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Both boundaries are required: a turn end alone does not end a cell."""
    workdir = tmp_path / "task"
    records = [
        _cell("launch", _computed_launch(PYTEST, workdir)),
        _cell_output("launch", _yielded("cell-y")),
        _task_complete(),
        _cell("ruff", _literal(RUFF, workdir)),
    ]
    _, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "block"
    assert "cannot prove its working directory" in (response.reason or "")


def test_compaction_without_task_complete_still_refuses_a_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Compaction can fall inside a live turn, before a wait on the same cell."""
    workdir = tmp_path / "task"
    records = [
        _cell("launch", _computed_launch(PYTEST, workdir)),
        _cell_output("launch", _yielded("cell-y")),
        _compacted(),
        _cell("ruff", _literal(RUFF, workdir)),
    ]
    _, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "block"
    assert "cannot prove its working directory" in (response.reason or "")


def test_cell_yielded_after_the_turn_end_survives_a_later_compaction(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Per cell: a cell yielded after task_complete was never ended by that turn."""
    workdir = tmp_path / "task"
    records = [
        _cell("launch", _computed_launch(PYTEST, workdir)),
        _cell_output("launch", _yielded("cell-y")),
        _task_complete(),
        _cell("next", _computed_launch(PYTEST, workdir)),
        _cell_output("next", _yielded("cell-z")),
        _compacted(),
        _cell("ruff", _literal(RUFF, workdir)),
    ]
    _, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "block"
    assert "cannot prove its working directory" in (response.reason or "")


def test_wait_that_reyields_an_ended_cell_keeps_later_literal_cwd_proof_refused(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """A late wait proves the cell is live again: the ended cell must stay tracked."""
    workdir = tmp_path / "task"
    records = [
        _cell("launch", _computed_launch(PYTEST, workdir)),
        _cell_output("launch", _yielded("cell-y")),
        _task_complete(),
        _compacted(),
        *_wait_result("wait", "cell-y", _yielded("cell-y")),
        _cell("ruff", _literal(RUFF, workdir)),
    ]
    _, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "block"
    assert "cannot prove its working directory" in (response.reason or "")


def test_wait_that_completes_an_ended_cell_admits_a_later_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """A wait that observes the cell finish leaves nothing running to refuse for."""
    workdir = tmp_path / "task"
    records = [
        _cell("launch", _computed_launch(PYTEST, workdir)),
        _cell_output("launch", _yielded("cell-y")),
        _task_complete(),
        _compacted(),
        *_wait_result("wait", "cell-y", "Script completed\nWall time 0.0 seconds\nOutput:\n"),
        _cell("ruff", _literal(RUFF, workdir)),
    ]
    event, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


def test_synthetic_sticky_latch_is_released_by_task_complete_then_compaction(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Synthetic guard, not the probe shape: a typed write latches the open cell until both boundaries end it."""
    workdir = tmp_path / "task"
    records = [
        _cell("launch", _launch(PYTEST, workdir)),
        _cell_output("launch", _yielded("cell-y")),
        _cell("typed", _typed_into_dead_session(SESSION_ID)),
        _cell_output("typed", "Output:\nls\n"),
        _task_complete(),
        _compacted(),
        _cell("ruff", _literal(RUFF, workdir)),
    ]
    event, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


def test_synthetic_re_yielding_wait_re_tracks_a_released_sticky_cell(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """Synthetic guard: a wait that re-yields the released cell makes it pending again, so it refuses."""
    workdir = tmp_path / "task"
    records = [
        _cell("launch", _launch(PYTEST, workdir)),
        _cell_output("launch", _yielded("cell-y")),
        _cell("typed", _typed_into_dead_session(SESSION_ID)),
        _cell_output("typed", "Output:\nls\n"),
        _task_complete(),
        _compacted(),
        *_wait_result("wait", "cell-y", _yielded("cell-y")),
        _cell("ruff", _literal(RUFF, workdir)),
    ]
    _, response = _hook(event_handlers, tmp_path, records, command=RUFF)
    assert response.decision == "block"
    assert "cannot prove its working directory" in (response.reason or "")
