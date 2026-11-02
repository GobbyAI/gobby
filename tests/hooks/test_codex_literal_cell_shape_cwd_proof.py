"""A literal launch cell keeps its own cwd proof when harmless code sits beside it.

An empty ``write_stdin`` poll only reads output from a running session. A loop that does not
contain the launch cannot run it again. Neither may make the launch's working directory
unprovable. An input poll, a launch inside a loop or callback, and a computed workdir stay
fail closed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.codex_impl.cell_shell_reach import may_reach_shell
from gobby.adapters.codex_impl.execution_chain import extract_functions_exec_command
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.codex_impl.literal_shell_calls import literal_shell_calls
from gobby.hooks._path_scope import current_tool_cwd
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookResponse

pytestmark = pytest.mark.unit

PYTEST = "uv run --no-sync pytest tests/hooks/test_codex_orphan_cell_cwd_proof.py -q"

INERT_POLLS = [
    'text(await tools.write_stdin({session_id:901,chars:"",'
    "yield_time_ms:1000,max_output_tokens:7000}));",
    'text(await tools.write_stdin({"session_id":901,"chars":""}));',
]

NON_INERT_POLLS = [
    'text(await tools.write_stdin({session_id:901,chars:"ls\\n"}));',
    'text(await tools.write_stdin({session_id:901,chars:"",chars:"ls\\n"}));',
    'const c = "ls"; text(await tools.write_stdin({session_id:901,chars:c}));',
    # An empty chars key can be overridden by a spread, a computed key or a quoted key.
    'const args = {chars:"ls\\n"}; text(await tools.write_stdin({chars:"",...args}));',
    'text(await tools.write_stdin({chars:"",\'chars\':"ls\\n"}));',
    'text(await tools.write_stdin({chars:"",["chars"]:"ls\\n"}));',
    'const k = "chars"; text(await tools.write_stdin({chars:"",[k]:"ls\\n"}));',
    'text(await tools.write_stdin({chars:"",\\u0063hars:"ls\\n"}));',
]

SEND = 'await tools.mcp__gobby__send_message({target:"parent",content:"x"});'
SEND_LOOP = "for (const t of [1, 2]) { " + SEND + " } "

# LAUNCH stands for the literal exec. Its cwd is provable wherever it runs once, at top level.
PROVEN_SHAPES = [
    SEND + SEND_LOOP + "LAUNCH",
    "LAUNCH" + SEND_LOOP,
]

# Each of these runs the launch zero or many times, so its own cwd cannot be tied to one call.
REFUSED_SHAPES = [
    "for (const t of [1, 2]) { LAUNCH } ",
    "for (const t of [1, 2]) LAUNCH ",
    "[1, 2].forEach((t) => { LAUNCH });",
    "const run = () => LAUNCH",
    "function run() { LAUNCH } run();",
]


def _literal(command: str, workdir: Path) -> str:
    return (
        f"text(await tools.exec_command({{cmd:{json.dumps(command)},"
        f"workdir:{json.dumps(str(workdir))},yield_time_ms:1000,max_output_tokens:1800}}));"
    )


def _computed(command: str, workdir: Path) -> str:
    return (
        f"const w = {json.dumps(str(workdir))}; "
        f"text(await tools.exec_command({{cmd:{json.dumps(command)},workdir:w,"
        "yield_time_ms:1000,max_output_tokens:1800}));"
    )


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


@pytest.mark.parametrize("poll", INERT_POLLS)
def test_literal_launch_with_inert_poll_proves_its_cwd(
    event_handlers: EventHandlers, tmp_path: Path, poll: str
) -> None:
    workdir = tmp_path / "task"
    records = [_call("launch", _literal(PYTEST, workdir) + poll)]
    event, response = _hook(event_handlers, tmp_path, records)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


@pytest.mark.parametrize("poll", NON_INERT_POLLS)
def test_launch_with_input_poll_stays_unproven(
    event_handlers: EventHandlers, tmp_path: Path, poll: str
) -> None:
    workdir = tmp_path / "task"
    records = [_call("launch", _literal(PYTEST, workdir) + poll)]
    event, response = _hook(event_handlers, tmp_path, records)
    assert response.decision == "block", current_tool_cwd(event.data)
    assert "cannot prove its working directory" in (response.reason or "")


@pytest.mark.parametrize("shape", PROVEN_SHAPES)
def test_top_level_launch_beside_a_loop_proves_its_cwd(
    event_handlers: EventHandlers, tmp_path: Path, shape: str
) -> None:
    workdir = tmp_path / "task"
    cell = shape.replace("LAUNCH", _literal(PYTEST, workdir))
    event, response = _hook(event_handlers, tmp_path, [_call("launch", cell)])
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


@pytest.mark.parametrize("shape", REFUSED_SHAPES)
def test_launch_inside_a_repeated_scope_stays_unproven(
    event_handlers: EventHandlers, tmp_path: Path, shape: str
) -> None:
    workdir = tmp_path / "task"
    cell = shape.replace("LAUNCH", _literal(PYTEST, workdir))
    event, response = _hook(event_handlers, tmp_path, [_call("launch", cell)])
    assert response.decision == "block", current_tool_cwd(event.data)
    assert "cannot prove its working directory" in (response.reason or "")


@pytest.mark.parametrize("shape", ["LAUNCH", *PROVEN_SHAPES])
def test_computed_workdir_stays_unproven(
    event_handlers: EventHandlers, tmp_path: Path, shape: str
) -> None:
    workdir = tmp_path / "task"
    cell = shape.replace("LAUNCH", _computed(PYTEST, workdir))
    event, response = _hook(event_handlers, tmp_path, [_call("launch", cell)])
    assert response.decision == "block", current_tool_cwd(event.data)
    assert "cannot prove its working directory" in (response.reason or "")


def test_plain_dollar_home_in_a_literal_still_proves_its_cwd(
    event_handlers: EventHandlers, tmp_path: Path
) -> None:
    workdir = tmp_path / "task"
    command = f"{PYTEST} --basetemp=$HOME/pytest"
    records = [_call("launch", _literal(command, workdir))]
    event, response = _hook(event_handlers, tmp_path, records, command=command)
    assert response.decision == "allow", response.reason
    assert current_tool_cwd(event.data) == workdir


# A comma or an if/else makes the launch part of one unbraced loop body, so it repeats.
UNBRACED_LOOP_LAUNCHES = [
    "for (const t of [1, 2]) await tools.noop(), LAUNCH",
    "for (const t of [1, 2]) if (t) await tools.noop(); else LAUNCH",
]
# A poll value that runs code is not an inert empty chars key.
EXECUTABLE_POLLS = [
    'text(await tools.write_stdin({chars:"",session_id:eval("1")}));',
    'text(await tools.write_stdin({chars:"",yield_time_ms:f()}));',
]
# An identifier that starts with `else` is not the keyword, so the loop body ends at its `;`.
ELSE_PREFIX_IDENTIFIER = (
    "let elsewhere; for (const t of [1, 2]) await tools.noop(); elsewhere = LAUNCH"
)


@pytest.mark.parametrize("shape", UNBRACED_LOOP_LAUNCHES)
def test_launch_in_an_unbraced_loop_body_is_not_a_literal_shell_call(shape: str) -> None:
    cell = shape.replace("LAUNCH", _literal(PYTEST, Path("/task")))
    assert literal_shell_calls(cell) is None
    assert extract_functions_exec_command(cell) is None


@pytest.mark.parametrize("poll", EXECUTABLE_POLLS)
def test_poll_with_an_executable_value_is_not_blanked_as_inert(poll: str) -> None:
    assert may_reach_shell(poll)
    assert literal_shell_calls(_literal(PYTEST, Path("/task")) + poll) is None


def test_identifier_with_an_else_prefix_does_not_extend_a_loop_body() -> None:
    cell = ELSE_PREFIX_IDENTIFIER.replace("LAUNCH", _literal(PYTEST, Path("/task")))
    calls = literal_shell_calls(cell)
    assert calls is not None
    assert [call["cmd"] for call in calls] == [PYTEST]
