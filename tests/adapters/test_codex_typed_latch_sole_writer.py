"""Typed latches: a failed sole literal write releases; only an observed exit clears a live cell."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gobby.adapters.codex_impl.cell_shell_reach import sole_typed_session
from gobby.adapters.codex_impl.execution_chain import ExecutionChainCorrelator

pytestmark = pytest.mark.unit

LAUNCH = (
    'const r = await tools.exec_command({cmd:"pytest",workdir:"/w",tty:true,'
    "yield_time_ms:1000}); text(r);"
)
YIELDED = "Script running with cell ID cell-y\nWall time 31.2 seconds\nOutput:\n"
LITERAL_WRITE = 'text(await tools.write_stdin({session_id:5,chars:"ls\\n"}));'
# The computed write types into live shell 801 before the literal write to session 5 fails.
COMPUTED_THEN_LITERAL = (
    'await tools["write_"+"stdin"]({session_id:801,chars:"rm x\\n"}); '
    'await tools.write_stdin({session_id:5,chars:"ls\\n"});'
)
FAILED_WRITE = [
    {"type": "input_text", "text": "Script failed\nWall time 0.1 seconds\nOutput:\n"},
    {"type": "input_text", "text": "Error: Unknown process id 5"},
]


def _pending_launch() -> ExecutionChainCorrelator:
    chain = ExecutionChainCorrelator()
    assert chain.register_call("launch", "exec", LAUNCH)
    chain.resolve_output("launch", YIELDED)
    return chain


def test_failed_literal_write_beside_a_computed_write_keeps_its_latch() -> None:
    """The computed write may have typed into a live shell, so its failed sibling proves nothing."""
    chain = _pending_launch()
    chain.register_call("typed", "exec", COMPUTED_THEN_LITERAL)
    chain.resolve_output("typed", FAILED_WRITE)
    assert chain.snapshot_state()["sticky_unattributed"] == ["launch"]


def test_failed_literal_write_that_is_the_only_typing_code_releases_its_latch() -> None:
    """A failed write that types nothing must not hold a later literal proof open."""
    chain = _pending_launch()
    chain.register_call("typed", "exec", LITERAL_WRITE)
    assert chain.snapshot_state()["sticky_unattributed"] == ["launch"]
    chain.resolve_output("typed", FAILED_WRITE)
    assert chain.snapshot_state()["sticky_unattributed"] == []


def test_compaction_keeps_a_typed_latch_on_its_live_cell() -> None:
    """A compaction does not end a live cell, so its typed latch holds."""
    chain = _pending_launch()
    chain.register_call("typed", "exec", LITERAL_WRITE)
    chain.abandon_unresolved_calls()
    state = chain.snapshot_state()
    assert "cell-y" in state["cells"]
    assert state["sticky_unattributed"] == ["launch"]


def test_unreadable_wait_result_keeps_a_typed_latch_and_its_cell() -> None:
    """A wait with no readable outcome is no proof the cell exited: the cell stays live."""
    chain = _pending_launch()
    chain.register_call("typed", "exec", LITERAL_WRITE)
    assert chain.register_call("wait", "wait", json.dumps({"cell_id": "cell-y"}))
    chain.resolve_output("wait", "Output:\npartial")
    state = chain.snapshot_state()
    assert "cell-y" in state["cells"]
    assert state["sticky_unattributed"] == ["launch"]


@pytest.mark.parametrize(
    ("arguments", "session_id", "expected"),
    [
        pytest.param(LITERAL_WRITE, "5", "5", id="literal-write-only"),
        pytest.param(f"{LAUNCH} {LITERAL_WRITE}", "5", "5", id="exec-command-beside-it"),
        pytest.param(COMPUTED_THEN_LITERAL, "5", None, id="computed-write-beside-it"),
        pytest.param(
            'const c = `l${"s"}\\n`; text(await tools.write_stdin({session_id:5,chars:c}));',
            "5",
            None,
            id="computed-chars",
        ),
        pytest.param({"session_id": 5, "chars": "ls"}, "5", "5", id="direct-arguments"),
        pytest.param(LITERAL_WRITE, None, None, id="no-session"),
    ],
)
def test_sole_typed_session_names_only_a_lone_literal_writer(
    arguments: Any, session_id: str | None, expected: str | None
) -> None:
    assert sole_typed_session(arguments, session_id) == expected
