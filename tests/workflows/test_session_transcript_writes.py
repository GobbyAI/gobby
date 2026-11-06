"""Edit and Write cannot write any session transcript, the writer's own included (#25793).

A background call settles only on a notice row the CLI appends to its owner's transcript.
An agent that writes a transcript itself can forge that row, so the write gate refuses
every transcript, not only another session's.

The behaviour tests evaluate a synthetic definition that carries the gate's condition. The
bundled definitions are enumerated generically: each file parses, each rule passes the
engine's dry run, and each rule whose condition is the transcript gate blocks before a tool.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.hooks.normalization import normalize_tool_fields
from gobby.utils.strict_yaml import load_strict_yaml
from gobby.workflows.condition_helpers_paths import session_transcript_write
from gobby.workflows.rule_validation_yaml import normalize_rule_yaml_entry, validate_rule_yaml
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
from gobby.workflows.sync_rules import get_bundled_rules_paths

pytestmark = pytest.mark.unit

GATE = "session_transcript_write"
SYNTHETIC_DEFINITION: dict[str, Any] = {
    "event": "before_tool",
    "enabled": True,
    "when": f"{GATE}(event.data, tool_input)",
    "effects": [{"type": "block", "reason": "Session transcripts are written only by their CLI."}],
}
OWN_SESSION = "11111111-1111-4111-8111-111111111111"
OTHER_SESSION = "22222222-2222-4222-8222-222222222222"
PROJECT_DIR = "-Users-josh-worktrees-gobby-task-25793"


def _bundled_rule_files() -> list[Path]:
    """Every active bundled rule file, enumerated as the sync reads them."""
    files: list[Path] = []
    for root in get_bundled_rules_paths():
        for path in sorted(root.rglob("*.yaml")):
            if "deprecated" not in path.relative_to(root).parts:
                files.append(path)
    return files


BUNDLED_RULE_FILES = _bundled_rule_files()


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
    """Evaluate the synthetic definition's condition the way the engine does, with its helpers."""
    evaluator = SafeExpressionEvaluator(
        {
            "event": SimpleNamespace(data=event_data),
            "tool_input": {"file_path": file_path},
        },
        build_condition_helpers(),
    )
    return bool(evaluator.evaluate(SYNTHETIC_DEFINITION["when"]))


def _bundled_rules() -> list[tuple[Path, str, dict[str, Any], dict[str, Any]]]:
    """Each bundled rule as the sync reads it: file defaults applied, row metadata split out."""
    rules = []
    for path in BUNDLED_RULE_FILES:
        data = load_strict_yaml(path.read_text(encoding="utf-8"))
        for name, rule in data["rules"].items():
            body, metadata = normalize_rule_yaml_entry(rule, defaults=data)
            rules.append((path, str(name), body, metadata))
    return rules


@pytest.mark.parametrize("path", BUNDLED_RULE_FILES, ids=lambda path: path.name)
def test_bundled_rule_file_parses_and_passes_the_engine_dry_run(path: Path) -> None:
    reports = validate_rule_yaml(path.read_text(encoding="utf-8"), source=str(path))

    assert reports, path
    for report in reports:
        report.raise_for_errors()


def test_each_bundled_rule_gating_transcripts_blocks_before_a_tool() -> None:
    gates = [
        (path, name, body, metadata)
        for path, name, body, metadata in _bundled_rules()
        if GATE in str(body.get("when", ""))
    ]

    assert gates, "no bundled rule gates session transcripts"
    for path, name, body, metadata in gates:
        assert body["event"] == "before_tool", (path, name)
        assert metadata.get("enabled", True) is True, (path, name)
        assert [effect["type"] for effect in body["effects"]] == ["block"], (path, name)


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
