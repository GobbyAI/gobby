"""A refused cwd proof logs the guard that refused it, so the daemon log names the cause."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.codex_impl.hooks_adapter import _pending_shell_workdir

pytestmark = pytest.mark.unit

LOGGER = "gobby.adapters.codex_impl.hooks_adapter"
RUFF = "uv run ruff check --fix src/gobby/workflows/imports.py"
OTHER = "uv run ruff check --fix src/gobby/workflows/other.py"
COMPUTED = 'const c = "ls"; text(await tools.exec_command({cmd:c,workdir:"/task"}));'


def _literal(command: str) -> str:
    return (
        f"text(await tools.exec_command({{cmd:{json.dumps(command)},"
        'workdir:"/task",yield_time_ms:1000,max_output_tokens:1800}));'
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


@pytest.mark.parametrize(
    ("records", "command", "complete", "guard"),
    [
        ([_call("a", _literal(RUFF))], RUFF, False, "incomplete provider record"),
        ([_call("a", _literal(RUFF))], OTHER, True, "0 pending calls match"),
        ([_call("a", _literal(RUFF)), _call("b", _literal(RUFF))], RUFF, True, "2 pending calls"),
        (
            [_call("a", COMPUTED), _call("b", _literal(RUFF))],
            RUFF,
            True,
            "unattributed pending call",
        ),
    ],
)
def test_refused_cwd_proof_logs_the_guard_that_refused_it(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    records: list[dict[str, Any]],
    command: str,
    complete: bool,
    guard: str,
) -> None:
    transcript = tmp_path / "rollout.jsonl"
    text = "".join(json.dumps(record) + "\n" for record in records)
    transcript.write_text(text if complete else text.rstrip("\n"))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        cwd = _pending_shell_workdir(
            {"transcript_path": str(transcript), "tool_input": {"command": command}}
        )
    assert cwd is None
    assert guard in caplog.text
