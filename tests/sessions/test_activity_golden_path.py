"""Five-provider golden path for transcript-grounded session activity."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from gobby.config.sessions import SessionSummaryConfig
from gobby.memory.digest import (
    DigestPair,
    _build_turn_record,
    _build_turn_record_prompt,
    _extract_digest_pairs,
)
from gobby.sessions.analyzer_turns import analyzer_turns_from_transcript
from gobby.sessions.summarize import (
    FeatureConfigProtocol,
    SummarySourceContext,
    _generate_session_summary_core,
    build_summary_source_context,
)
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts import tool_activity as tool_activity_module
from gobby.sessions.transcripts.base import TranscriptParser
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

SOURCES = ("claude", "codex", "grok", "qwen", "droid")
FIXTURE_DIR = Path(__file__).parent / "transcripts" / "fixtures" / "golden_path"
WIDGET_PATH = "src/pkg/widget.py"
PYTEST_COMMAND = "uv run pytest -k widget"
COMMIT_COMMAND = 'git commit -m "[gobby-#777] feat: widget"'
COMMIT_OUTPUT = "[0.5.0 abc1234] [gobby-#777] feat: widget"
FAILED_COMMAND = "cat /nonexistent/widget.log"
FAILED_ERROR = "cat: /nonexistent/widget.log: No such file or directory"
READ_COMMAND = "cat README.md"
READ_SENTINEL = "SENTINEL-README-OUTPUT"
PENDING_COMMAND = "tail -f /var/log/widget.log"

SUMMARY_PROMPT = """Ground truth:
{structured_context}

Digest:
{transcript_summary}

Recent digest activity:
{recent_digest_turns}

## Current State

Write the grounded current state.

## Next Steps

Write the grounded next steps.
"""

FIXED_SUMMARY = """## Current State

The transcript-grounded widget workflow is complete, including its edit, validation, task
operations, commit, failure evidence, and compaction activity.

## Next Steps

Use the recorded facts for the next session and retain the successful validation evidence.
"""


@dataclass(frozen=True)
class _PromptCall:
    caller: str
    prompt: str


class _RecordingLLMService:
    def __init__(self) -> None:
        self.calls: list[_PromptCall] = []

    async def call_json_feature(
        self,
        _config: Any,
        prompt: str,
        **kwargs: Any,
    ) -> dict[str, str]:
        self.calls.append(_PromptCall(str(kwargs.get("caller") or ""), prompt))
        return {
            "turn_markdown": (
                "User requested the widget golden path. The agent completed the recorded "
                "transcript-grounded workflow and preserved its activity evidence."
            ),
            "title_candidate": "Widget Golden Path",
        }

    async def call_feature(
        self,
        feature_config: FeatureConfigProtocol,
        prompt: str,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        *,
        caller: str | None = None,
        cwd: str | None = None,
        output_validator: Callable[[str], str | None] | None = None,
    ) -> str:
        del feature_config, system_prompt, max_tokens, cwd, output_validator
        self.calls.append(_PromptCall(caller or "", prompt))
        return FIXED_SUMMARY

    def prompt_for(self, caller: str) -> str:
        prompts = [call.prompt for call in self.calls if call.caller == caller]
        assert len(prompts) == 1, f"expected one {caller} prompt, got {len(prompts)}"
        return prompts[0]


class _SummaryManager:
    db = None

    def __init__(self, session: SimpleNamespace) -> None:
        self.session = session

    def get(self, session_id: str) -> SimpleNamespace | None:
        return self.session if session_id == self.session.id else None

    def update_summary(
        self,
        session_id: str,
        summary_path: str | None = None,
        summary_markdown: str | None = None,
    ) -> SimpleNamespace | None:
        del summary_path
        if session_id != self.session.id:
            return None
        self.session.summary_markdown = summary_markdown
        return self.session

    def update_status(self, session_id: str, status: str) -> SimpleNamespace | None:
        if session_id != self.session.id:
            return None
        self.session.status = status
        return self.session

    def persist_summary_state(
        self,
        session_id: str,
        *,
        summary_markdown: str,
        generation_mode: str,
        source_context_hash: str | None = None,
        source_digest_turn_count: int | None = None,
        metadata_json: dict[str, object] | None = None,
        summary_path: str | None = None,
    ) -> SimpleNamespace | None:
        del metadata_json, summary_path
        if session_id != self.session.id:
            return None
        self.session.summary_markdown = summary_markdown
        self.session.summary_generation_mode = generation_mode
        self.session.summary_source_context_hash = source_context_hash
        self.session.summary_digest_turn_count = source_digest_turn_count
        return self.session


@dataclass(frozen=True)
class _SummaryRun:
    pairs: list[DigestPair]
    source_context: SummarySourceContext
    llm: _RecordingLLMService


def _fixture_path(source: str) -> Path:
    path = FIXTURE_DIR / f"{source}.jsonl"
    assert path.is_file(), f"missing golden-path fixture: {path}"
    if source == "droid":
        sidecar = path.with_suffix(".settings.json")
        assert sidecar.is_file(), f"missing Droid sidecar: {sidecar}"
    return path


def _load_turns(source: str) -> tuple[Path, list[dict[str, Any]]]:
    path = _fixture_path(source)
    turns = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert turns, f"empty golden-path fixture: {path}"
    return path, turns


def _parser(source: str, path: Path) -> TranscriptParser:
    return get_parser(source, session_id=f"golden-{source}", transcript_path=path)


def _pair_text(pairs: list[DigestPair]) -> str:
    return "\n".join(part for pair in pairs for part in (pair.prompt, pair.response, pair.activity))


def _line_containing(ledger: str, needle: str) -> str:
    matches = [line for line in ledger.splitlines() if needle in line]
    assert len(matches) == 1, f"expected one ledger line containing {needle!r}: {ledger}"
    return matches[0]


def _turn_record_renderer(
    _template: str,
    values: dict[str, str],
    _db: HubDatabase,
) -> str:
    return _build_turn_record_prompt(values["prompt_text"], values["response_text"])


async def _seed_digest(
    pairs: list[DigestPair],
    llm: _RecordingLLMService,
) -> str:
    source_pairs = [
        (pair.prompt, "\n\n".join(part for part in (pair.response, pair.activity) if part))
        for pair in pairs
    ]
    with patch("gobby.memory.digest._render_prompt_template", side_effect=_turn_record_renderer):
        turn_record = await _build_turn_record(
            llm,
            object(),
            source_pairs,
            cast(HubDatabase, None),
        )
    return f"<!-- gobby:digest-turn:1 -->\n### Turn 1\n{turn_record.turn_markdown}"


async def _run_summary(source: str, tmp_path: Path) -> _SummaryRun:
    path, turns = _load_turns(source)
    pairs = _extract_digest_pairs(_parser(source, path), turns)
    llm = _RecordingLLMService()
    digest_markdown = await _seed_digest(pairs, llm)
    session = SimpleNamespace(
        id=f"golden-{source}",
        source=source,
        transcript_path=str(path),
        terminal_context=json.dumps({"cwd": str(tmp_path)}),
        digest_markdown=digest_markdown,
        last_turn_markdown=None,
        last_assistant_content=None,
        summary_markdown=None,
        summary_source_context_hash=None,
        summary_digest_turn_count=None,
    )
    manager = _SummaryManager(session)
    summary_config = SessionSummaryConfig(prompt=SUMMARY_PROMPT, candidates=["claude/haiku"])

    with (
        patch("gobby.sessions.summarize.require_local_session_ownership", return_value="local"),
        patch("gobby.sessions.summarize._enrich_git_context", new_callable=AsyncMock),
        patch("gobby.workflows.git_utils.get_file_changes", return_value=""),
        patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
        patch(
            "gobby.sessions.session_wiki_file.write_session_wiki_page",
            return_value={"written": True},
        ),
    ):
        source_context = await build_summary_source_context(
            session,
            db=None,
            session_manager=manager,
            session_summary_config=summary_config,
        )
        assert source_context is not None
        core_result = await _generate_session_summary_core(
            session.id,
            manager,
            llm_service=llm,
            session_summary_config=summary_config,
        )

    assert core_result.result["success"] is True, core_result.result
    assert core_result.full_markdown == FIXED_SUMMARY
    return _SummaryRun(pairs=pairs, source_context=source_context, llm=llm)


@pytest.mark.parametrize("source", SOURCES)
def test_digest_pairs_carry_activity_for_every_cli(source: str) -> None:
    path, turns = _load_turns(source)
    without_activity = _parser(source, path).extract_last_messages(
        turns,
        num_pairs=max(1, len(turns)),
        include_tool_activity=False,
    )
    with_activity = _parser(source, path).extract_last_messages(
        turns,
        num_pairs=max(1, len(turns)),
        include_tool_activity=True,
    )

    assert len(with_activity) == len(without_activity) == 4
    assert (
        [message["role"] for message in with_activity]
        == [message["role"] for message in without_activity]
        == ["user", "assistant", "user", "assistant"]
    )
    assert [message["content"] for message in with_activity] == [
        message["content"] for message in without_activity
    ]

    pairs = _extract_digest_pairs(_parser(source, path), turns)
    assert len(pairs) == 2
    turn_one = pairs[0].activity
    turn_two = pairs[1].activity
    for expected in (
        WIDGET_PATH,
        PYTEST_COMMAND,
        "mcp gobby-tasks:claim_task task_id=#777",
        "mcp gobby-tasks:close_task task_id=#777 commit_sha=abc1234",
        "→ commit abc1234",
        f"{FAILED_COMMAND} ! failed: {FAILED_ERROR}",
    ):
        assert expected in turn_one, f"{source}: missing {expected!r} from {turn_one}"
    assert "mcp gobby-sessions:compact_self" in turn_two


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.asyncio
async def test_summary_ground_truth_for_every_cli(source: str, tmp_path: Path) -> None:
    run = await _run_summary(source, tmp_path)
    prompt = run.llm.prompt_for("sessions.summary")
    context = run.source_context.handoff_ctx

    assert context.files_modified == [WIDGET_PATH]
    assert {(item["id"], item["action"]) for item in context.task_progress} >= {
        ("#777", "claim_task"),
        ("#777", "close_task"),
    }
    assert any(commit["hash"] == "abc1234" for commit in context.git_commits)
    assert any("compact_self" in activity for activity in context.recent_activity)
    for expected in (
        "Files Modified:",
        WIDGET_PATH,
        "Recent Commits:",
        "abc1234",
        "Task Progress:",
        "claim_task",
        "close_task",
        "#777",
        "Recent Activity:",
        "compact_self",
    ):
        assert expected in prompt, f"{source}: missing {expected!r} from summary prompt"


@pytest.mark.parametrize("source", SOURCES)
def test_failed_call_annotated_and_protected_for_every_cli(
    source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, turns = _load_turns(source)
    monkeypatch.setattr(tool_activity_module, "DIGEST_ACTIVITY_MAX_LINES", 5)
    monkeypatch.setattr(tool_activity_module, "DIGEST_ACTIVITY_TAIL_LINES", 0)

    ledger = _extract_digest_pairs(_parser(source, path), turns)[0].activity

    assert len(ledger.splitlines()) <= 5
    assert f"{FAILED_COMMAND} ! failed: {FAILED_ERROR}" in ledger
    assert READ_COMMAND not in ledger
    if source == "codex":
        application_failures = [
            item
            for turn in turns
            if isinstance((payload := turn.get("payload")), dict)
            and isinstance((item := payload.get("item")), dict)
            and item.get("type") == "McpToolCall"
            and isinstance(item.get("result"), dict)
            and item["result"].get("success") is False
        ]
        assert len(application_failures) == 1
        failed_item = application_failures[0]
        assert failed_item["status"] == "completed"
        assert failed_item["result"] == {"success": False, "error": FAILED_ERROR}


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.asyncio
async def test_successful_readonly_output_excluded_everywhere(
    source: str,
    tmp_path: Path,
) -> None:
    run = await _run_summary(source, tmp_path)
    pair_text = _pair_text(run.pairs)
    analyzer_text = json.dumps(run.source_context.turns, ensure_ascii=False)
    structured_context = str(run.source_context.summary_context["structured_context"])
    prompt_text = "\n".join(call.prompt for call in run.llm.calls)

    assert READ_SENTINEL not in pair_text
    assert READ_SENTINEL not in structured_context
    assert READ_SENTINEL not in prompt_text
    assert FAILED_ERROR in pair_text
    assert FAILED_ERROR in analyzer_text
    assert COMMIT_OUTPUT in analyzer_text
    assert FAILED_ERROR in run.llm.prompt_for("memory.turn_record")


@pytest.mark.parametrize("source", SOURCES)
def test_success_and_missing_result_distinguishable_for_every_cli(source: str) -> None:
    path, turns = _load_turns(source)
    ledger = _extract_digest_pairs(_parser(source, path), turns)[0].activity

    edit_line = _line_containing(ledger, WIDGET_PATH)
    pytest_line = _line_containing(ledger, PYTEST_COMMAND)
    pending_line = _line_containing(ledger, PENDING_COMMAND)
    assert "! failed:" not in edit_line and "(no result recorded)" not in edit_line
    assert "! failed:" not in pytest_line and "(no result recorded)" not in pytest_line
    assert pending_line.endswith("(no result recorded)")


def test_codex_mixed_window_keeps_unmatched_wrapper() -> None:
    path, turns = _load_turns("codex")
    parser = _parser("codex", path)
    pairs = _extract_digest_pairs(parser, turns)
    first_ledger = pairs[0].activity

    for expected in (
        WIDGET_PATH,
        PYTEST_COMMAND,
        "mcp gobby-tasks:claim_task task_id=#777",
        COMMIT_COMMAND,
        "mcp gobby-tasks:close_task task_id=#777 commit_sha=abc1234",
        FAILED_COMMAND,
        READ_COMMAND,
        PENDING_COMMAND,
    ):
        assert first_ledger.count(expected) == 1, first_ledger

    adapted = analyzer_turns_from_transcript(_parser("codex", path), turns)
    uses = [
        block
        for turn in adapted
        for block in turn["message"]["content"]
        if block["type"] == "tool_use"
    ]
    commands = [block["input"].get("command") for block in uses if block["name"] == "Bash"]
    assert commands.count(PYTEST_COMMAND) == 1
    assert commands.count(COMMIT_COMMAND) == 1
    assert commands.count(READ_COMMAND) == 1
    assert commands.count(PENDING_COMMAND) == 1
    assert sum(block["input"].get("command") == FAILED_COMMAND for block in uses) == 1
    assert sum(block["input"].get("file_path") == WIDGET_PATH for block in uses) == 1
    assert (
        sum(
            block["name"] == "mcp gobby-tasks:claim_task"
            and block["input"].get("task_id") == "#777"
            for block in uses
        )
        == 1
    )
    assert (
        sum(
            block["name"] == "mcp gobby-tasks:close_task"
            and block["input"].get("commit_sha") == "abc1234"
            for block in uses
        )
        == 1
    )
