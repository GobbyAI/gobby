"""A literal exec_command cell keeps its cwd proof while its rollout record is still being written.

The pending-call lookup reads the rollout at hook time. A record that is not yet newline
terminated, or not yet in the file, gets a bounded re-read. A record that never completes,
a computed workdir, and a duplicate match stay refused without a retry. Every refusal is
logged at WARNING, and so is a native node command that is not a proven literal cd.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from gobby.adapters.codex_impl import hooks_adapter
from gobby.adapters.codex_impl.hooks_adapter import _pending_shell_workdir

pytestmark = pytest.mark.unit

LOGGER = "gobby.adapters.codex_impl.hooks_adapter"
WORKDIR = "/Users/josh/.gobby/worktrees/gobby/task-25568-rule-validation"
RUFF = "uv run ruff check --fix src/gobby/workflows/imports.py tests/workflows/test_hooks.py"
RETRY_DELAY_SECONDS = 0.2
RETRIES = 2


def _literal(command: str) -> str:
    return (
        f"text(await tools.exec_command({{cmd:{json.dumps(command)},"
        f"workdir:{json.dumps(WORKDIR)},yield_time_ms:1000,max_output_tokens:3000}}));"
    )


def _computed(command: str) -> str:
    return (
        f"const w = {json.dumps(WORKDIR)}; "
        f"text(await tools.exec_command({{cmd:{json.dumps(command)},workdir:w,"
        "yield_time_ms:1000,max_output_tokens:3000}));"
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


def _lookup(transcript: Path, command: str = RUFF) -> str | None:
    return _pending_shell_workdir(
        {"transcript_path": str(transcript), "tool_input": {"command": command}}
    )


def _warned(caplog: pytest.LogCaptureFixture, text: str) -> bool:
    return any(
        record.levelno == logging.WARNING and text in record.getMessage()
        for record in caplog.records
    )


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record each retry wait instead of sleeping."""
    recorded: list[float] = []
    monkeypatch.setattr(time, "sleep", recorded.append)
    return recorded


def test_unterminated_literal_record_that_completes_within_the_bound_proves_its_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    record = json.dumps(_call("launch", _literal(RUFF)))
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(record)
    waits: list[float] = []

    def finish_the_write(seconds: float) -> None:
        waits.append(seconds)
        transcript.write_text(record + "\n")

    monkeypatch.setattr(time, "sleep", finish_the_write)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert _lookup(transcript) == WORKDIR
    assert waits == [RETRY_DELAY_SECONDS]
    assert not caplog.records


def test_record_that_never_appears_stays_refused_after_the_bound(
    tmp_path: Path, waits: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert _lookup(transcript) is None
    assert waits == [RETRY_DELAY_SECONDS] * RETRIES
    assert _warned(caplog, "0 pending calls match the command")


def test_unterminated_record_that_never_completes_stays_refused_after_the_bound(
    tmp_path: Path, waits: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(json.dumps(_call("launch", _literal(RUFF))))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert _lookup(transcript) is None
    assert waits == [RETRY_DELAY_SECONDS] * RETRIES
    assert _warned(caplog, "incomplete provider record")


def test_computed_workdir_stays_refused_without_a_retry(tmp_path: Path, waits: list[float]) -> None:
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(json.dumps(_call("launch", _computed(RUFF))) + "\n")
    assert _lookup(transcript) is None
    assert waits == []


def test_duplicate_match_stays_refused_without_a_retry(
    tmp_path: Path, waits: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    transcript = tmp_path / "rollout.jsonl"
    records = [_call("a", _literal(RUFF)), _call("b", _literal(RUFF))]
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert _lookup(transcript) is None
    assert waits == []
    assert _warned(caplog, "2 pending calls match the command")


def test_native_node_command_refusal_is_logged_as_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(hooks_adapter, "node_origin", lambda: True)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert _pending_shell_workdir({"tool_input": {"command": "ls"}}) is None
    assert any(record.levelno == logging.WARNING for record in caplog.records)
