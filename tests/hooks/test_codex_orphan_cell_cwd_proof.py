"""Cells that cannot reach a shell, and cells orphaned by compaction, must not block cwd proof."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.codex_impl import hooks_adapter
from gobby.adapters.codex_impl.execution_chain import ExecutionChainCorrelator
from gobby.adapters.codex_impl.hooks_adapter import (
    CodexHooksAdapter,
    _pending_shell_lookup,
    _pending_shell_workdir,
    _PendingLookup,
)
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


@pytest.mark.parametrize("parallel", [False, True])
def test_multi_call_cell_proves_each_literal_workdir(
    event_handlers: EventHandlers, tmp_path: Path, parallel: bool
) -> None:
    first = f"tools.exec_command({json.dumps({'cmd': PYTEST, 'workdir': str(tmp_path / 'task')})})"
    second = f"tools.exec_command({json.dumps({'cmd': 'git status', 'workdir': str(tmp_path / 'other')})})"
    source = (
        f"await Promise.allSettled([{first}, {second}]);"
        if parallel
        else f"text(await {first}); text(await {second});"
    )
    event, response = _hook(event_handlers, tmp_path, [_call("multi", source)])
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == tmp_path / "task"


@pytest.mark.parametrize("unsafe", ["duplicate", "computed", "loop"])
def test_multi_call_cell_refuses_ambiguous_provenance(
    event_handlers: EventHandlers, tmp_path: Path, unsafe: str
) -> None:
    source = _literal(PYTEST, tmp_path / "task")
    if unsafe == "duplicate":
        source += _literal(PYTEST, tmp_path / "other")
    elif unsafe == "computed":
        source += "await tools.exec_command({cmd: command, workdir: directory});"
    else:
        source = f"for (const value of values) {{ {source} }}" + _literal("git status", tmp_path)
    event, response = _hook(event_handlers, tmp_path, [_call("multi", source)])
    assert response.decision == "block", response.reason
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


TASK_COMMAND = (
    "DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test "
    "GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_team_epic_runbook.py -v"
)
LOOP_CELLS = {
    "for_loop": (
        'for (const i of [1]) { text(await tools.exec_command({cmd:"uv run pytest",'
        'workdir:"/elsewhere"})); }'
    ),
    "map_loop": (
        '[1].map(() => text(await tools.exec_command({cmd:"uv run pytest",workdir:"/elsewhere"})));'
    ),
    "promise_all": (
        'await Promise.all([tools.exec_command({cmd:"uv run pytest",workdir:"/elsewhere"})]);'
    ),
    # A call repeated inside a template interpolation is invisible to a code-only view, which
    # sees only the outer call. The map must stay visible to the scaffold scan.
    "hidden_call_under_map": (
        '`${[1, 2].map(() => tools.exec_command({cmd:"uv run pytest",workdir:"/elsewhere"}))}`;'
        ' text(await tools.exec_command({cmd:"uv run pytest",workdir:"/elsewhere"}));'
    ),
}


def _contract_cell(command: str, workdir: Path | str) -> str:
    """One exec_command with literal cmd and workdir, plus the output-budget keys."""
    return (
        f"text(await tools.exec_command({{cmd:{json.dumps(command)},"
        f"workdir:{json.dumps(str(workdir))},max_output_tokens:3000,yield_time_ms:1000}}));"
    )


@pytest.mark.parametrize(
    ("command", "segment"),
    [
        (TASK_COMMAND, "task-25455-update-testteamepicrunbookpy-for-the-v5"),
        (TASK_COMMAND, "task-do-review"),
        (TASK_COMMAND, "task-while-review"),
        ("for f in tests/a.py; do uv run pytest $f; done", "task"),
    ],
)
def test_contract_cell_proves_cwd_when_loop_words_are_literal_data(
    event_handlers: EventHandlers, tmp_path: Path, command: str, segment: str
) -> None:
    """Loop words in a literal workdir or command are data, not a repeated exec_command."""
    workdir = tmp_path / segment
    records = [_call("contract", _contract_cell(command, workdir))]
    event, response = _hook(event_handlers, tmp_path, records, command=command)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


@pytest.mark.parametrize("name", list(LOOP_CELLS))
def test_loop_in_cell_code_still_fails_closed(
    event_handlers: EventHandlers, tmp_path: Path, name: str
) -> None:
    workdir = tmp_path / "task"
    records = [
        _call("loop", LOOP_CELLS[name]),
        _call("pytest", _contract_cell(TASK_COMMAND, workdir)),
    ]
    event, response = _hook(event_handlers, tmp_path, records, command=TASK_COMMAND)
    assert response.decision == "block", current_tool_cwd(event.data)
    assert "cannot prove its working directory" in (response.reason or "")


# Replay of the reviewer's rollout for task #25842: a yielded cell of two exec_command calls
# with no workdir, waited on twice; then a refused single-call cell; then a compliant cell.
REPLAY_DIFF = (
    "git diff 0.5.0...048e8bd2eb95e93ca25b35b30aa600371c9b5841 -- src/gobby/storage/plans.py "
    "src/gobby/mcp_proxy/tools/plans/__init__.py "
    "crates/gcore/migrations/490_grant_plan_coverage_reads.sql docs/contracts/plan-coverage.md"
)
REPLAY_OCR = (
    "ocr delegate preview --format json --from 0.5.0 --to 048e8bd2eb95e93ca25b35b30aa600371c9b5841"
)
REPLAY_PYTEST = (
    "DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test "
    "GOBBY_TEST_PROTECT=1 GOBBY_TEST_GDAEMON=checkout uv run pytest "
    "tests/mcp_proxy/test_plans_linked_checkout.py tests/storage/test_storage_plans.py "
    "tests/storage/test_plan_coverage_read_grant.py "
    "tests/storage/test_postgres_agent_authorization.py "
    "tests/fixtures/test_hang_guard.py -p no:cacheprovider -q -m 'not e2e'"
)
CELL_ONE = (
    f"text(await tools.exec_command({{cmd:{json.dumps(REPLAY_PYTEST)},"
    "yield_time_ms:1000,max_output_tokens:1500}));"
    'text((await tools.mcp__gobby__call_tool({server_name:"gobby-results",'
    'tool_name:"get_tool_result",arguments:{result_id:"4c073743-a578-4a7f-8452-bfe6ddf6fe56",'
    "offset:2000,limit:1800}})).structuredContent);"
)
YIELD_TEN = "Script running with cell ID 10\nWall time 31.2 seconds\nOutput:\n"
REFUSED_ONE = (
    "Script failed\nWall time 5.0 seconds\nOutput:\nScript error:\n"
    "Command blocked by PreToolUse hook: Gobby cannot prove its working directory."
)


def _unscoped(command: str, max_output_tokens: int) -> str:
    """One exec_command with no workdir, as a non-compliant cell writes it."""
    return (
        f"text(await tools.exec_command({{cmd:{json.dumps(command)},"
        f"max_output_tokens:{max_output_tokens}}}));"
    )


def _wait(call_id: str, cell_id: str) -> dict[str, Any]:
    """Codex's wait tool: a function_call whose JSON arguments name the yielded cell."""
    arguments = json.dumps({"cell_id": cell_id, "yield_time_ms": 30000})
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "wait",
            "call_id": call_id,
            "arguments": arguments,
        },
    }


def _wait_output(call_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": call_id, "output": text},
    }


def test_yielded_unscoped_batch_does_not_block_later_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """The yielded batch cannot have launched the probe, so it must not poison later proofs."""
    workdir = tmp_path / "task-25804"
    records = [
        _call("cell10", _unscoped(REPLAY_DIFF, 8500) + _unscoped(REPLAY_OCR, 1000)),
        _output("cell10", YIELD_TEN),
        _wait("wait1", "10"),
        _wait_output("wait1", YIELD_TEN),
        _wait("wait2", "10"),
        _wait_output("wait2", YIELD_TEN),
        _call("cell1", CELL_ONE),
        _output("cell1", REFUSED_ONE),
        _call("cell2", _literal(REPLAY_PYTEST, workdir)),
    ]
    event, response = _hook(event_handlers, tmp_path, records, command=REPLAY_PYTEST)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


def test_yielded_unscoped_batch_member_does_not_prove_its_own_command(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """A member of the yielded batch is never validated: a bare run of it stays refused."""
    records = [
        _call("cell10", _unscoped(REPLAY_PYTEST, 8500) + _unscoped(REPLAY_OCR, 1000)),
        _output("cell10", YIELD_TEN),
    ]
    event, response = _hook(event_handlers, tmp_path, records, command=REPLAY_PYTEST)
    assert response.decision == "block", current_tool_cwd(event.data)
    assert "cannot prove its working directory" in (response.reason or "")


@pytest.mark.parametrize("name", list(UNKNOWN_CELLS))
def test_yielded_unknown_cell_blocks_later_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path, name: str
) -> None:
    workdir = tmp_path / "task"
    records = [
        _call("unknown", UNKNOWN_CELLS[name]),
        _output("unknown", YIELD_TEN),
        _call("pytest", _literal(PYTEST, workdir)),
    ]
    event, response = _hook(event_handlers, tmp_path, records, command=PYTEST)
    assert response.decision == "block", current_tool_cwd(event.data)
    assert "cannot prove its working directory" in (response.reason or "")


YIELDED_NON_COMPLIANT = {
    "variable_workdir": (
        f"text(await tools.exec_command({{cmd:{json.dumps(REPLAY_DIFF)},workdir:dir}}));"
        f"text(await tools.exec_command({{cmd:{json.dumps(REPLAY_OCR)},max_output_tokens:1000}}));"
    ),
    "load_helper": (
        "const job = await load('job');"
        " text(await tools.exec_command({cmd: job.cmd, workdir: job.dir}));"
        f"text(await tools.exec_command({{cmd:{json.dumps(REPLAY_OCR)}}}));"
    ),
}


@pytest.mark.parametrize("name", list(YIELDED_NON_COMPLIANT))
def test_yielded_non_compliant_batch_still_blocks_later_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path, name: str
) -> None:
    workdir = tmp_path / "task"
    records = [
        _call("batch", YIELDED_NON_COMPLIANT[name]),
        _output("batch", YIELD_TEN),
        _call("pytest", _literal(REPLAY_PYTEST, workdir)),
    ]
    event, response = _hook(event_handlers, tmp_path, records, command=REPLAY_PYTEST)
    assert response.decision == "block", current_tool_cwd(event.data)
    assert "cannot prove its working directory" in (response.reason or "")


def test_refused_unscoped_batch_does_not_block_later_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    """The two-cell shape of the report: a refused multi-call cell, then one literal cell."""
    workdir = tmp_path / "task"
    records = [
        _call("refused", _unscoped(REPLAY_DIFF, 8500) + _unscoped(REPLAY_OCR, 1000)),
        _output("refused", REFUSED_ONE),
        _call("cell2", _literal(REPLAY_PYTEST, workdir)),
    ]
    event, response = _hook(event_handlers, tmp_path, records, command=REPLAY_PYTEST)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


RUFF_SHAPES = (
    "uv run --no-sync ruff check src/gobby/adapters/codex_impl",
    "uv run ruff check src/gobby/adapters/codex_impl",
)


@pytest.mark.parametrize("ruff", RUFF_SHAPES)
@pytest.mark.parametrize("prior", ["alone", "after_yielded_batch", "after_refused_cell"])
def test_literal_ruff_check_proves_its_workdir(
    event_handlers: EventHandlers, tmp_path: Path, prior: str, ruff: str
) -> None:
    """Report 756fe608: a literal, read-only ruff check was refused in a Codex developer tree."""
    workdir = tmp_path / "task"
    records: list[dict[str, Any]] = []
    if prior == "after_yielded_batch":
        records += [
            _call("cell10", _unscoped(REPLAY_PYTEST, 8500) + _unscoped(REPLAY_OCR, 1000)),
            _output("cell10", YIELD_TEN),
        ]
    elif prior == "after_refused_cell":
        records += [_call("cell1", CELL_ONE), _output("cell1", REFUSED_ONE)]
    records.append(_call("ruff", _literal(ruff, workdir)))
    event, response = _hook(event_handlers, tmp_path, records, command=ruff)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


TYPED = "uv run --no-sync pytest tests/hooks/test_typed.py -q"
STDIN_OUTPUT = "Wall time 1.0 seconds\nOutput:\n"
SHELL_SESSION = 901


def _stdin(call_id: str, chars: str) -> dict[str, Any]:
    """A direct write_stdin into the shell session a yielded cell left running."""
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "write_stdin",
            "call_id": call_id,
            "arguments": json.dumps({"session_id": SHELL_SESSION, "chars": chars}),
        },
    }


def _nested_stdin(call_id: str, chars: str) -> dict[str, Any]:
    """A write_stdin nested in an exec cell: code mode types into the same shell session."""
    source = (
        f"text(await tools.write_stdin({{session_id:{SHELL_SESSION},chars:{json.dumps(chars)}}}));"
    )
    return _call(call_id, source)


# Each way typed input reaches a shell session: (record builder, its output builder).
STDIN_FORMS = {"direct": (_stdin, _wait_output), "nested": (_nested_stdin, _output)}


@pytest.mark.parametrize("chars", [TYPED + "\n", "\n"], ids=["command", "enter"])
@pytest.mark.parametrize("form", list(STDIN_FORMS))
def test_typed_input_to_unmapped_shell_blocks_later_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path, form: str, chars: str
) -> None:
    """Typed input, even a bare Enter, reaches a shell the chain never mapped; the batch may run it."""
    workdir = tmp_path / "task"
    build_stdin, output_stdin = STDIN_FORMS[form]
    records = [
        _call("cell10", _unscoped(REPLAY_DIFF, 8500) + _unscoped(REPLAY_OCR, 1000)),
        _output("cell10", YIELD_TEN),
        build_stdin("stdin", chars),
        output_stdin("stdin", STDIN_OUTPUT),
        _call("probe", _literal(REPLAY_PYTEST, workdir)),
    ]
    event, response = _hook(event_handlers, tmp_path, records, command=REPLAY_PYTEST)
    assert response.decision == "block", current_tool_cwd(event.data)
    assert "cannot prove its working directory" in (response.reason or "")


@pytest.mark.parametrize("form", list(STDIN_FORMS))
def test_empty_poll_to_unmapped_shell_keeps_later_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path, form: str
) -> None:
    """An empty poll types nothing, so it cannot start a command and must not set the marker."""
    workdir = tmp_path / "task"
    build_stdin, output_stdin = STDIN_FORMS[form]
    records = [
        _call("cell10", _unscoped(REPLAY_DIFF, 8500) + _unscoped(REPLAY_OCR, 1000)),
        _output("cell10", YIELD_TEN),
        build_stdin("poll", ""),
        output_stdin("poll", STDIN_OUTPUT),
        _call("probe", _literal(REPLAY_PYTEST, workdir)),
    ]
    event, response = _hook(event_handlers, tmp_path, records, command=REPLAY_PYTEST)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


STICKY_REFUSAL = "typed input reached an unmapped session; a pending cell is unattributed"
TYPED_JS = json.dumps(TYPED + "\n")
UNMAPPED_WRITE = f"text(await tools.write_stdin({{session_id:{SHELL_SESSION},chars:{TYPED_JS}}}));"
# Cells that may type into the yielded batch's unmapped shell. Only a literal empty poll of a
# literal session types nothing, so each of these must mark the batch.
TYPING_CELLS = {
    "concat": f'text(await tools.write_stdin({{session_id:{SHELL_SESSION},chars:"" + "ls\\n"}}));',
    "concat_tight": f'text(await tools.write_stdin({{session_id:{SHELL_SESSION},chars:""+"id"}}));',
    "concat_method": (
        f'text(await tools.write_stdin({{session_id:{SHELL_SESSION},chars:"".concat("id")}}));'
    ),
    "computed_session": (
        f"const sid={SHELL_SESSION}; "
        f"text(await tools.write_stdin({{session_id:sid,chars:{TYPED_JS}}}));"
    ),
    "computed_session_poll": (
        f'const sid={SHELL_SESSION}; text(await tools.write_stdin({{session_id:sid,chars:""}}));'
    ),
    "leading_zero_session": (
        f"text(await tools.write_stdin({{session_id:0{SHELL_SESSION},chars:{TYPED_JS}}}));"
    ),
    "spread_chars": (
        f"const extra={{chars:{TYPED_JS}}}; "
        f"text(await tools.write_stdin({{session_id:{SHELL_SESSION},...extra}}));"
    ),
    "aliased": (
        f"const w=tools.write_stdin; text(await w({{session_id:{SHELL_SESSION},chars:{TYPED_JS}}}));"
    ),
    "beside_poll": (
        f'text(await tools.write_stdin({{session_id:{SHELL_SESSION},chars:""}})); {UNMAPPED_WRITE}'
    ),
    "beside_literal_exec": _literal("git status", "/opt/elsewhere") + UNMAPPED_WRITE,
    "opaque": 'eval(atob("dG9vbHM="));',
}


@pytest.mark.parametrize("shape", list(TYPING_CELLS))
def test_nested_input_that_is_not_a_literal_empty_poll_blocks_later_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path, shape: str
) -> None:
    """Computed chars, a computed session, aliasing or opaque code may type into the shell."""
    workdir = tmp_path / "task"
    records = [
        _call("cell10", _unscoped(REPLAY_DIFF, 8500) + _unscoped(REPLAY_OCR, 1000)),
        _output("cell10", YIELD_TEN),
        _call("stdin", TYPING_CELLS[shape]),
        _output("stdin", STDIN_OUTPUT),
        _call("probe", _literal(REPLAY_PYTEST, workdir)),
    ]
    event, response = _hook(event_handlers, tmp_path, records, command=REPLAY_PYTEST)
    assert response.decision == "block", current_tool_cwd(event.data)
    assert "cannot prove its working directory" in (response.reason or "")
    assert _lookup(tmp_path, records, REPLAY_PYTEST).refusal == STICKY_REFUSAL


@pytest.mark.parametrize(
    "cell",
    [
        f'text(await tools.write_stdin({{session_id:{SHELL_SESSION},chars:""}})); '
        f'text(await tools.write_stdin({{session_id:{SHELL_SESSION},chars:""}}));',
        _literal("git status", "/opt/elsewhere")
        + f'text(await tools.write_stdin({{session_id:{SHELL_SESSION},chars:""}}));',
    ],
    ids=["two_polls", "poll_beside_literal_exec"],
)
def test_literal_empty_polls_keep_later_literal_cwd_proof(
    event_handlers: EventHandlers, tmp_path: Path, cell: str
) -> None:
    """Literal empty polls type nothing, however many a cell holds or what it launches beside."""
    workdir = tmp_path / "task"
    records = [
        _call("cell10", _unscoped(REPLAY_DIFF, 8500) + _unscoped(REPLAY_OCR, 1000)),
        _output("cell10", YIELD_TEN),
        _call("stdin", cell),
        _output("stdin", STDIN_OUTPUT),
        _call("probe", _literal(REPLAY_PYTEST, workdir)),
    ]
    event, response = _hook(event_handlers, tmp_path, records, command=REPLAY_PYTEST)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


MAPPED_SESSION = 905
# Input to a session an attributed launch owns is attributed; only the rest of the cell can
# reach the unmapped shell. (cell, decision)
MAPPED_CELLS = {
    "literal": (
        f"text(await tools.write_stdin({{session_id:{MAPPED_SESSION},chars:{TYPED_JS}}}));",
        "allow",
    ),
    "quoted_id": (
        f'text(await tools.write_stdin({{session_id:"{MAPPED_SESSION}",chars:{TYPED_JS}}}));',
        "allow",
    ),
    "computed_suffix": (
        f"const x=0; "
        f"text(await tools.write_stdin({{session_id:{MAPPED_SESSION} + x,chars:{TYPED_JS}}}));",
        "block",
    ),
    "beside_opaque": (
        f'text(await tools.write_stdin({{session_id:{MAPPED_SESSION},chars:"ls\\n"}})); '
        'eval(atob("dG9vbHM="));',
        "block",
    ),
}


@pytest.mark.parametrize("shape", list(MAPPED_CELLS))
def test_input_to_a_mapped_session_marks_only_what_else_may_type(
    event_handlers: EventHandlers, tmp_path: Path, shape: str
) -> None:
    """A literal write to the mapped shell is attributed; a computed id or opaque code is not."""
    workdir = tmp_path / "task"
    cell, decision = MAPPED_CELLS[shape]
    launch = (
        'const r = await tools.exec_command({cmd:"zsh -i",'
        f"workdir:{json.dumps(str(workdir))},tty:true}}); text(r);"
    )
    records = [
        _call("cell10", _unscoped(REPLAY_DIFF, 8500) + _unscoped(REPLAY_OCR, 1000)),
        _output("cell10", YIELD_TEN),
        _call("shell", launch),
        _output("shell", json.dumps({"session_id": MAPPED_SESSION, "output": "ready"})),
        _call("stdin", cell),
        _output("stdin", STDIN_OUTPUT),
        _call("probe", _literal(REPLAY_PYTEST, workdir)),
    ]
    event, response = _hook(event_handlers, tmp_path, records, command=REPLAY_PYTEST)
    assert response.decision == decision, response.reason
    if decision == "allow":
        assert current_tool_cwd(event.data) == workdir
    else:
        assert _lookup(tmp_path, records, REPLAY_PYTEST).refusal == STICKY_REFUSAL


def _lookup(tmp_path: Path, records: list[dict[str, Any]], command: str) -> _PendingLookup:
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    return _pending_shell_lookup(str(transcript), command)


def _yield_text(cell_id: int) -> str:
    return f"Script running with cell ID {cell_id}\nWall time 1.0 seconds\nOutput:\n"


def test_literal_ruff_check_refused_once_pending_history_overflows(tmp_path: Path) -> None:
    """Fail closed by design: an evicted yielded cell may still run a shell with no known command."""
    workdir = tmp_path / "task"
    records: list[dict[str, Any]] = []
    for index in range(65):
        records += [
            _call(f"launch{index}", _literal(f"sleep {index}", workdir)),
            _output(f"launch{index}", _yield_text(index)),
        ]
    records.append(_call("ruff", _literal(RUFF_SHAPES[0], workdir)))
    lookup = _lookup(tmp_path, records, RUFF_SHAPES[0])
    assert lookup.workdir is None
    assert lookup.refusal == "pending calls overflowed or are ambiguous"


def test_literal_ruff_check_refused_when_its_record_never_lands(tmp_path: Path) -> None:
    """A probe with no rollout record is refused and stays retryable, not proven by absence."""
    lookup = _lookup(tmp_path, [], RUFF_SHAPES[0])
    assert lookup.workdir is None
    assert lookup.refusal == "0 pending calls match the command"
    assert lookup.retryable


@pytest.mark.parametrize("tail", ["missing", "partial"])
@pytest.mark.parametrize("ruff", RUFF_SHAPES)
def test_literal_ruff_check_proves_cwd_once_its_unwritten_record_lands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ruff: str, tail: str
) -> None:
    """The rollout tail lags the PreToolUse hook; the record lands during the retry window."""
    workdir = tmp_path / "task"
    line = json.dumps(_call("ruff", _literal(ruff, workdir)))
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("" if tail == "missing" else line[: len(line) // 2])

    def land_record(_seconds: float) -> None:
        transcript.write_text(line + "\n")

    monkeypatch.setattr(time, "sleep", land_record)
    cwd = _pending_shell_workdir(
        {"transcript_path": str(transcript), "tool_input": {"command": ruff}}
    )
    assert cwd == str(workdir)


def test_refused_cwd_proof_logs_only_redacted_command_head(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Diagnostics name the session and a redacted command head, never argument values or paths."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("")
    input_data = {
        "session_id": "sess-redact",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path / "launcher"),
        "tool_input": {"command": REPLAY_PYTEST},
    }
    caplog.set_level(logging.WARNING, logger=hooks_adapter.__name__)
    assert _pending_shell_workdir(input_data) is None
    [message] = [
        record.getMessage()
        for record in caplog.records
        if "cwd proof refused" in record.getMessage()
    ]
    assert "sess-redact" in message
    assert "0 pending calls match the command" in message
    assert "head=uv" in message
    assert "argc=" in message
    for secret in ("gobby_test", "127.0.0.1", "tests/", "not e2e", str(tmp_path)):
        assert secret not in message
