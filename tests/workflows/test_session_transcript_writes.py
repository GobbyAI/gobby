"""Edit and Write cannot write any session transcript, the writer's own included (#25793).

A background call settles only on a notice row the CLI appends to its owner's transcript.
An agent that writes a transcript itself can forge that row, so the write gate refuses
every transcript, not only another session's.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from gobby.hooks.normalization import normalize_tool_fields
from gobby.workflows.condition_helpers_paths import session_transcript_write
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers

pytestmark = pytest.mark.unit

RULE_NAME = "block-session-transcript-writes"
RULE_FILE = (
    Path(__file__).resolve().parents[2]
    / "src/gobby/install/shared/workflows/rules/task-enforcement/block-session-transcript-writes.yaml"
)
OWN_SESSION = "11111111-1111-4111-8111-111111111111"
OTHER_SESSION = "22222222-2222-4222-8222-222222222222"
PROJECT_DIR = "-Users-josh-worktrees-gobby-task-25793"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _event(home: Path, tool: str, file_path: str) -> dict[str, Any]:
    return normalize_tool_fields(
        {
            "tool_name": tool,
            "tool_input": {"file_path": file_path, "content": "x"},
            "cwd": str(home),
            "project_path": str(home),
        }
    )


def _rule_when(event_data: dict[str, Any], file_path: str) -> bool:
    """Evaluate the bundled rule's condition the way the engine does, with its helpers."""
    rule = yaml.safe_load(RULE_FILE.read_text(encoding="utf-8"))["rules"][RULE_NAME]
    evaluator = SafeExpressionEvaluator(
        {
            "event": SimpleNamespace(data=event_data),
            "tool_input": {"file_path": file_path},
        },
        build_condition_helpers(),
    )
    return bool(evaluator.evaluate(rule["when"]))


def test_helper_refuses_transcripts_under_the_configured_claude_config_dir(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = home / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    transcript = config / "projects" / PROJECT_DIR / f"{OTHER_SESSION}.jsonl"
    event = _event(home, "Write", str(transcript))

    assert session_transcript_write(event, {"file_path": str(transcript)}) is True
    assert _rule_when(event, str(transcript)) is True


def test_helper_refuses_every_session_transcript_the_writer_own_included(home: Path) -> None:
    projects = home / ".claude" / "projects" / PROJECT_DIR
    paths = [
        projects / f"{OWN_SESSION}.jsonl",
        projects / f"{OTHER_SESSION}.jsonl",
        projects / OWN_SESSION / "subagents" / "agent-1.jsonl",
        home / ".claude" / "projects" / "-other-project" / f"{OTHER_SESSION}.jsonl",
    ]
    for path in paths:
        assert session_transcript_write(_event(home, "Edit", str(path)), {}) is True, path


@pytest.mark.parametrize("tool", ["Edit", "Write"])
def test_edit_and_write_cannot_touch_another_seats_transcript(home: Path, tool: str) -> None:
    # Seat A's file tools reach seat B's transcript: the gate names no owner, so it refuses.
    other = home / ".claude" / "projects" / PROJECT_DIR / f"{OTHER_SESSION}.jsonl"
    event = _event(home, tool, str(other))

    assert _rule_when(event, str(other)) is True


@pytest.mark.parametrize("tool", ["Edit", "Write"])
def test_edit_and_write_cannot_touch_the_writers_own_transcript(home: Path, tool: str) -> None:
    own = home / ".claude" / "projects" / PROJECT_DIR / f"{OWN_SESSION}.jsonl"

    assert _rule_when(_event(home, tool, str(own)), str(own)) is True


def test_a_bash_redirect_into_another_seats_transcript_is_refused_at_the_hook(home: Path) -> None:
    # The hook sees the redirect's target; the sandbox that stops a Bash child is a separate layer.
    other = home / ".claude" / "projects" / PROJECT_DIR / f"{OTHER_SESSION}.jsonl"
    event = normalize_tool_fields(
        {
            "tool_name": "Bash",
            "tool_input": {"command": f"echo probe >> {other}"},
            "cwd": str(home),
            "project_path": str(home),
        }
    )

    assert _rule_when(event, str(other)) is True


def test_writes_that_are_not_transcripts_stay_allowed(home: Path) -> None:
    projects = home / ".claude" / "projects" / PROJECT_DIR
    allowed = [
        projects / "memory" / "MEMORY.md",
        projects / "notes.txt",
        home / ".claude" / "settings.json",
        home / ".claude" / "projects-notes" / f"{OTHER_SESSION}.jsonl",
        home / "worktree" / "src" / "app.py",
    ]
    for path in allowed:
        event = _event(home, "Write", str(path))
        assert session_transcript_write(event, {}) is False, path
        assert _rule_when(event, str(path)) is False, path


def test_helper_refuses_a_transcript_whose_suffix_or_root_differs_only_in_case(home: Path) -> None:
    # The volume folds case, so these name the real transcript; the gate must not compare them
    # as different paths.
    transcript = home / ".claude" / "projects" / PROJECT_DIR / f"{OTHER_SESSION}.jsonl"
    variants = [transcript.with_suffix(".JSONL"), Path(str(transcript).upper())]
    for path in variants:
        event = _event(home, "Write", str(path))
        assert session_transcript_write(event, {"file_path": str(path)}) is True, path


def test_rule_file_refuses_transcripts_through_the_engine_helpers(home: Path) -> None:
    rule = yaml.safe_load(RULE_FILE.read_text(encoding="utf-8"))
    body = rule["rules"][RULE_NAME]

    assert body["event"] == "before_tool"
    assert body["enabled"] is True
    assert [effect["type"] for effect in body["effects"]] == ["block"]
    assert "session_transcript_write" in body["when"]
