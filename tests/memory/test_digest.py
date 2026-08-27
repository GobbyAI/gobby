"""Tests for memory digest pipeline functions.

Relocated from tests/workflows/test_memory_actions.py as part of dead-code cleanup.
"""

import asyncio
import hashlib
import json
import logging
import threading
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.watchdog.codex import CodexTranscriptWatchdogReader
from gobby.config.sessions import DigestConfig
from gobby.llm.base import LLMProviderCancellation
from gobby.memory.digest import (
    DigestPair,
    UndigestedBatch,
    _build_turn_record_prompt,
    _extract_digest_pairs,
    _get_next_turn_number,
    _read_last_turn_from_transcript,
    _read_undigested_turns,
    _should_update_digest_title,
    _turn_record_source_texts,
    _validate_turn_record_payload,
    build_turn_and_digest,
)
from gobby.memory.generation_schemas import TURN_RECORD_SCHEMA
from gobby.memory.title_heuristics import (
    is_template_placeholder,
    normalize_title_candidate,
)
from gobby.sessions.transcripts.grok import GrokTranscriptParser


@pytest.mark.asyncio
async def test_extract_digest_pairs_includes_tool_activity() -> None:
    fixture = (
        Path(__file__).parents[1]
        / "sessions"
        / "transcripts"
        / "fixtures"
        / "grok_audit"
        / "10711"
        / "updates.jsonl"
    )
    turns = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]

    activity = "\n".join(
        pair.activity for pair in _extract_digest_pairs(GrokTranscriptParser(), turns)
    )

    assert "search_replace" in activity
    assert "mcp gobby-tasks:claim_task" in activity

    last_turn = await _read_last_turn_from_transcript(str(fixture), "grok")

    assert last_turn is not None
    assert "[tool activity]" not in "\n".join(last_turn)
    assert "search_replace" not in last_turn[1]
    assert "gobby-tasks:claim_task" not in last_turn[1]


def test_turn_record_prompts_carry_tool_activity_instruction() -> None:
    bundled = (
        Path(__file__).parents[2]
        / "src"
        / "gobby"
        / "install"
        / "shared"
        / "prompts"
        / "memory"
        / "turn_record.md"
    ).read_text(encoding="utf-8")
    inline = _build_turn_record_prompt("Prompt", "Response")

    for prompt in (bundled, inline):
        normalized = " ".join(prompt.split())
        assert "[tool activity]" in normalized
        assert "narration that contradicts it is wrong" in normalized
        assert "(no result recorded)" in normalized


async def _assert_event_loop_progresses[T](
    operation: Awaitable[T],
    started: threading.Event,
    release: threading.Event,
) -> T:
    """Prove a deliberately blocked persistence call is not on the event loop."""
    safety_release = threading.Timer(1.0, release.set)
    safety_release.start()
    task = asyncio.ensure_future(operation)
    try:
        observed = await asyncio.wait_for(asyncio.to_thread(started.wait, 0.5), timeout=0.75)
        assert observed, "persistence call did not start"
        await asyncio.sleep(0)
        assert not release.is_set(), "persistence blocked the event loop until the safety timeout"
        release.set()
        return await task
    finally:
        release.set()
        safety_release.cancel()
        if not task.done():
            task.cancel()


pytestmark = pytest.mark.unit

_CLAUDE_FIXTURE = Path(__file__).parent / "fixtures" / "claude_transcript_titles.jsonl"


@dataclass(frozen=True)
class _DigestTestConfig:
    digest: DigestConfig
    session_summary: object | None = None


def _digest_config(**kwargs: object) -> _DigestTestConfig:
    return _DigestTestConfig(digest=DigestConfig(candidates=["claude/haiku"], **kwargs))


def _turn_record_payload(
    turn_markdown: str = "User asked to fix a bug. Agent found the root cause.",
    title_candidate: str | None = "Fix Auth Bug",
) -> dict[str, str]:
    payload: dict[str, str] = {"turn_markdown": turn_markdown}
    if title_candidate is not None:
        payload["title_candidate"] = title_candidate
    return payload


def _write_interrupted_codex_transcript(path: Path) -> None:
    import json

    records = [
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Investigate the title"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "The title is still provisional."}],
                "phase": "commentary",
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "turn_aborted", "turn_id": "turn-1"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-2"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Continue after interrupt"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records))


def test_turn_record_contract_log_omits_full_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = {"turn_markdown": "x" * 220 + " sensitive-tail"}
    response_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    response_sha = hashlib.sha256(response_text.encode("utf-8")).hexdigest()

    with caplog.at_level(logging.DEBUG, logger="gobby.memory.digest"):
        with pytest.raises(ValueError, match="invalid JSON contract"):
            _validate_turn_record_payload(payload, exchange_count=1)

    assert response_sha in caplog.text
    assert f"response_chars={len(response_text)}" in caplog.text
    assert response_text[:40] in caplog.text
    assert "sensitive-tail" not in caplog.text


class TestGetNextTurnNumber:
    """Tests for _get_next_turn_number helper."""

    def test_empty_digest(self) -> None:
        assert _get_next_turn_number(None) == 1
        assert _get_next_turn_number("") == 1

    def test_no_turn_headers(self) -> None:
        assert _get_next_turn_number("### Turn 87\nModel-authored heading") == 1

    def test_single_turn(self) -> None:
        digest = "<!-- gobby:digest-turn:1 -->\n### Turn 1\nSome content here"
        assert _get_next_turn_number(digest) == 2

    def test_multiple_turns(self) -> None:
        digest = (
            "<!-- gobby:digest-turn:1 -->\n### Turn 1\nFirst turn\n\n"
            "<!-- gobby:digest-turn:2 -->\n### Turn 2\nSecond turn\n\n"
            "<!-- gobby:digest-turn:3 -->\n### Turn 3\nThird"
        )
        assert _get_next_turn_number(digest) == 4

    def test_non_sequential_turns(self) -> None:
        digest = (
            "<!-- gobby:digest-turn:1 -->\n### Turn 1\nFirst\n\n"
            "<!-- gobby:digest-turn:5 -->\n### Turn 5\nFifth"
        )
        assert _get_next_turn_number(digest) == 6


class TestNormalizeTitleCandidate:
    """Tests for LLM title candidate cleanup."""

    @pytest.mark.parametrize(
        "candidate",
        [
            "/gobby coderabbit",
            "$gobby coderabbit",
            "/help",
            "$skill",
        ],
    )
    def test_rejects_command_only_candidates(self, candidate: str) -> None:
        assert normalize_title_candidate(candidate) is None

    @pytest.mark.parametrize(
        ("candidate", "expected"),
        [
            ("/gobby coderabbit fix review comments", "Fix review comments"),
            ("$gobby coderabbit fix review comments", "Fix review comments"),
            ("/skill do thing", "Do thing"),
            ("$skill do thing", "Do thing"),
        ],
    )
    def test_strips_command_prefix_from_candidates(
        self,
        candidate: str,
        expected: str,
    ) -> None:
        assert normalize_title_candidate(candidate) == expected

    def test_preserves_plain_candidate(self) -> None:
        assert normalize_title_candidate("Digest JSON Titles") == "Digest JSON Titles"

    @pytest.mark.parametrize(
        "candidate",
        [
            "2026-07-24 Digest Titles",
            "14:30 Digest Titles",
            "#9550 Codex",
            "Fix titles └── now",
            "Fix titles 🚀",
            "Fix • titles",
        ],
    )
    def test_rejects_metadata_and_decorative_junk(self, candidate: str) -> None:
        assert normalize_title_candidate(candidate) is None

    def test_normalizes_whitespace_without_rejecting_unicode_words(self) -> None:
        assert normalize_title_candidate("  Café\nsearch\tfix  ") == "Café search fix"


class TestShouldUpdateDigestTitle:
    """Tests for digest title ownership policy."""

    def test_missing_title_attrs_are_treated_as_untitled(self) -> None:
        class LegacySession:
            pass

        assert _should_update_digest_title(LegacySession()) is True

    @pytest.mark.parametrize("title_source", ["llm", "provisional", None])
    def test_every_automatic_title_is_replaceable(self, title_source: str | None) -> None:
        session = MagicMock()
        session.title = "#42 Codex"
        session.title_source = title_source

        assert _should_update_digest_title(session) is True

    @pytest.mark.parametrize(
        ("title", "title_source"),
        [
            ("Manual Title", "manual"),
            ("", "manual"),
        ],
    )
    def test_manual_title_is_the_only_digest_override(
        self,
        title: str,
        title_source: str,
    ) -> None:
        session = MagicMock()
        session.title = title
        session.title_source = title_source

        assert _should_update_digest_title(session) is False


class TestIsTemplatePlaceholder:
    """Tests for placeholder detection including angle-bracket patterns."""

    @pytest.mark.parametrize(
        "value",
        [
            "<user_query>",
            "<user_prompt>",
            "<prompt>",
            "<input>",
            "<USER_QUERY>",
        ],
    )
    def test_angle_bracket_placeholders(self, value: str) -> None:
        assert is_template_placeholder(value) is True

    def test_bracketed_numbered_placeholder(self) -> None:
        assert is_template_placeholder("[3-5 word session title]") is True

    def test_normal_text_not_placeholder(self) -> None:
        assert is_template_placeholder("Fix the auth bug") is False

    def test_angle_bracket_domain_term_not_placeholder(self) -> None:
        assert is_template_placeholder("<vector_db>") is False


class TestReadLastTurnFromTranscript:
    """Tests for _read_last_turn_from_transcript helper."""

    @pytest.mark.asyncio
    async def test_nonexistent_file(self) -> None:
        prompt, response = await _read_last_turn_from_transcript(
            "/nonexistent/path.jsonl", "claude"
        )
        assert prompt == ""
        assert response == ""

    @pytest.mark.asyncio
    async def test_empty_file(self, tmp_path) -> None:
        jsonl_file = tmp_path / "transcript.jsonl"
        jsonl_file.write_text("")
        prompt, response = await _read_last_turn_from_transcript(str(jsonl_file), "claude")
        assert prompt == ""
        assert response == ""

    @pytest.mark.asyncio
    async def test_claude_transcript(self, tmp_path) -> None:
        """Test reading from a Claude-format JSONL transcript."""
        import json

        jsonl_file = tmp_path / "transcript.jsonl"
        turns = [
            {"message": {"role": "user", "content": "Hello, what is 2+2?"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "2+2 equals 4."}],
                }
            },
        ]
        jsonl_file.write_text("\n".join(json.dumps(t) for t in turns))

        prompt, response = await _read_last_turn_from_transcript(str(jsonl_file), "claude")
        assert prompt == "Hello, what is 2+2?"
        assert response == "2+2 equals 4."

    @pytest.mark.asyncio
    async def test_multiple_turns_returns_last(self, tmp_path) -> None:
        """Test that only the last user/assistant pair is returned."""
        import json

        transcript = tmp_path / "transcript.jsonl"
        lines = [
            {"message": {"role": "user", "content": "Fix the auth bug in login.py"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "I found the issue in login.py line 42. The token validation "
                            "was missing a check for expired tokens. Fixed it.",
                        }
                    ],
                }
            },
            {"message": {"role": "user", "content": "Add tests for the fix"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "Created test_login.py with 3 test cases covering "
                            "token expiry, invalid tokens, and valid tokens.",
                        }
                    ],
                }
            },
        ]
        transcript.write_text("\n".join(json.dumps(line) for line in lines))

        prompt, response = await _read_last_turn_from_transcript(str(transcript), "claude")
        assert prompt == "Add tests for the fix"
        assert (
            response
            == "Created test_login.py with 3 test cases covering token expiry, invalid tokens, and valid tokens."
        )


class TestBuildTurnAndDigest:
    """Tests for build_turn_and_digest pipeline function."""

    @pytest.fixture
    def mock_memory_manager(self):
        mm = MagicMock()
        mm.config.enabled = True
        mm.content_exists.return_value = False
        mock_memory = MagicMock()
        mock_memory.id = "mem-123"
        mm.create_memory = AsyncMock(return_value=mock_memory)
        return mm

    @pytest.fixture
    def mock_session_manager(self):
        sm = MagicMock()
        session = MagicMock()
        session.id = "session-123"
        session.transcript_path = None
        session.source = "claude"
        session.digest_markdown = None
        session.summary_digest_turn_count = 5
        session.title = None
        session.title_source = None
        session.seq_num = 42
        session.terminal_context = None
        sm.get.return_value = session
        sm.update_last_turn_markdown.return_value = session
        sm.update_digest_markdown.return_value = session
        sm.update_title.return_value = session
        sm.persist_digest_state.return_value = session
        return sm

    @pytest.fixture
    def mock_llm_service(self):
        service = MagicMock()
        service.call_json_feature = AsyncMock(
            side_effect=[
                _turn_record_payload(
                    "User asked to fix a bug. Agent found the root cause in auth.py line 42.",
                    "Fix Auth Bug",
                ),
            ]
        )
        return service

    def test_turn_record_source_texts_keep_complete_pairs(self) -> None:
        prompt = "p" * 4500
        response = "r" * 8500

        assert _turn_record_source_texts([(prompt, response)]) == (prompt, response)
        combined, empty = _turn_record_source_texts([(prompt, response), ("next", "ok")])
        assert empty == ""
        assert prompt in combined
        assert response in combined
        assert "next" in combined

    def test_turn_prompt_instructs_titles_to_ignore_router_commands(self) -> None:
        turn_prompt = _build_turn_record_prompt("$gobby coderabbit fix comments", "Done")

        assert "/gobby coderabbit" in turn_prompt
        assert "$gobby coderabbit" in turn_prompt
        assert "/help" in turn_prompt
        assert "$skill" in turn_prompt
        assert "Never" in turn_prompt
        assert "`/` or `$`" in turn_prompt

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self, mock_session_manager, mock_llm_service):
        mm = MagicMock()
        mm.config.enabled = False
        result = await build_turn_and_digest(
            memory_manager=mm,
            session_manager=mock_session_manager,
            session_id="s1",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_without_llm(self, mock_memory_manager, mock_session_manager):
        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="s1",
            llm_service=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_session(self, mock_memory_manager, mock_llm_service):
        sm = MagicMock()
        sm.get.return_value = None
        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=sm,
            session_id="nonexistent",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_no_content(
        self, mock_memory_manager, mock_session_manager, mock_llm_service
    ):
        """No transcript and no prompt_text means no content to process."""
        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="s1",
            prompt_text=None,
            llm_service=mock_llm_service,
            config=_digest_config(),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_lifecycle_commands(
        self, mock_memory_manager, mock_session_manager, mock_llm_service
    ):
        for cmd in ["/clear", "/exit", "/compact"]:
            result = await build_turn_and_digest(
                memory_manager=mock_memory_manager,
                session_manager=mock_session_manager,
                session_id="s1",
                prompt_text=cmd,
                llm_service=mock_llm_service,
                config=_digest_config(),
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_skips_synthetic_daemon_prompts(
        self, mock_memory_manager, mock_session_manager, mock_llm_service
    ):
        """A daemon wake ping with no transcript content produces no digest turn."""
        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="s1",
            prompt_text="Message from Gobby daemon: New activity available.",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )
        assert result is None
        mock_llm_service.call_json_feature.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_pipeline(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Test the full pipeline with prompt_text provided."""
        digest_config = _digest_config()
        digest_config = _DigestTestConfig(
            digest=digest_config.digest,
            session_summary=object(),
        )
        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the authentication bug in auth.py",
            llm_service=mock_llm_service,
            config=digest_config,
        )

        assert result is not None
        assert result["turn_num"] == 1
        assert result["turn_length"] > 0
        assert result["digest_length"] > 0
        assert result["title"] == "Fix Auth Bug"

        # Verify digest state was persisted atomically.
        mock_session_manager.persist_digest_state.assert_called_once()
        call_args = mock_session_manager.persist_digest_state.call_args
        assert call_args.args == ("session-123",)
        last_turn = call_args.kwargs["last_turn_markdown"]
        assert "bug" in last_turn.lower() or "auth" in last_turn.lower()

        # Verify digest was appended with turn header.
        digest_content = call_args.kwargs["digest_markdown"]
        assert "<!-- gobby:digest-turn:1 -->" in digest_content
        assert "### Turn 1" in digest_content
        assert "root cause in auth.py line 42" in digest_content

        # Verify title was included in the same persistence call.
        assert call_args.kwargs["title"] == "Fix Auth Bug"
        assert call_args.kwargs["title_source"] == "llm"
        assert mock_llm_service.call_json_feature.await_count == 1
        llm_call = mock_llm_service.call_json_feature.await_args
        assert llm_call.args[0] is digest_config.digest
        assert "Fix the authentication bug in auth.py" in llm_call.args[1]
        assert llm_call.kwargs["json_schema"] == TURN_RECORD_SCHEMA
        assert llm_call.kwargs["caller"] == "memory.turn_record"
        refresh_names = [
            call.kwargs.get("name")
            for call in mock_memory_manager.schedule_background_task.call_args_list
        ]
        # session_summary is unset on _digest_config(), so refresh is skipped.
        assert not any(
            isinstance(name, str) and name.startswith("session-summary-refresh")
            for name in refresh_names
        )

    @pytest.mark.asyncio
    async def test_schedules_summary_refresh_when_digest_grows_past_watermark(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ) -> None:
        session = mock_session_manager.get.return_value
        session.summary_digest_turn_count = 0
        session.summary_markdown = "## Current State\nCompact snapshot"
        digest_config = _digest_config()
        digest_config = _DigestTestConfig(
            digest=digest_config.digest,
            session_summary=object(),
        )

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the authentication bug in auth.py",
            llm_service=mock_llm_service,
            config=digest_config,
        )

        assert result is not None
        refresh_names = [
            call.kwargs.get("name")
            for call in mock_memory_manager.schedule_background_task.call_args_list
        ]
        assert "session-summary-refresh-session-123" in refresh_names

    @pytest.mark.asyncio
    async def test_does_not_schedule_summary_refresh_when_at_watermark(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ) -> None:
        session = mock_session_manager.get.return_value
        session.summary_digest_turn_count = 1
        session.summary_markdown = "## Current State\nCompact snapshot"
        digest_config = _DigestTestConfig(
            digest=_digest_config().digest,
            session_summary=object(),
        )

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the authentication bug in auth.py",
            llm_service=mock_llm_service,
            config=digest_config,
        )

        assert result is not None
        refresh_names = [
            call.kwargs.get("name")
            for call in mock_memory_manager.schedule_background_task.call_args_list
        ]
        assert not any(
            isinstance(name, str) and name.startswith("session-summary-refresh")
            for name in refresh_names
        )

    @pytest.mark.asyncio
    async def test_codex_turn_start_catches_up_once_without_completed_turn_recovery(
        self,
        tmp_path: Path,
        mock_memory_manager: MagicMock,
        mock_session_manager: MagicMock,
    ) -> None:
        transcript = tmp_path / "codex.jsonl"
        _write_interrupted_codex_transcript(transcript)
        transcript_before = transcript.read_text()
        session: MagicMock = mock_session_manager.get.return_value
        session.transcript_path = str(transcript)
        session.source = "codex"
        session.title = "#42 Codex"
        session.title_source = "provisional"
        session.last_digest_input_hash = None
        session.last_digested_pair_index = 0

        def persist_state(_session_id: str, **values: Any) -> MagicMock:
            session.last_turn_markdown = values["last_turn_markdown"]
            session.digest_markdown = values["digest_markdown"]
            session.last_digest_input_hash = values["last_digest_input_hash"]
            session.last_digested_pair_index = values["last_digested_pair_index"]
            if values["title"] is not None:
                session.title = values["title"]
                session.title_source = values["title_source"]
            return session

        mock_session_manager.persist_digest_state.side_effect = persist_state
        llm_service = MagicMock()
        llm_service.call_json_feature = AsyncMock(
            return_value=_turn_record_payload(
                "User asked why the title was still provisional after an interrupt.",
                "Interrupted Turn Titles",
            )
        )

        first = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            llm_service=llm_service,
            config=_digest_config(),
            catch_up=True,
        )
        repeated = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            llm_service=llm_service,
            config=_digest_config(),
            catch_up=True,
        )

        assert first is not None
        assert first["title"] == "Interrupted Turn Titles"
        assert repeated is None
        assert session.last_digested_pair_index == 1
        assert session.title == "Interrupted Turn Titles"
        assert session.title_source == "llm"
        mock_session_manager.persist_digest_state.assert_called_once()
        llm_service.call_json_feature.assert_awaited_once()
        assert transcript.read_text() == transcript_before
        assert '"type": "task_complete"' not in transcript_before
        watchdog_snapshot = await CodexTranscriptWatchdogReader().read(str(transcript))
        assert watchdog_snapshot.latest_turn_kind == "started"

    @pytest.mark.asyncio
    async def test_concurrent_turns_are_serialized_in_digest_order(
        self,
        mock_memory_manager,
        mock_session_manager,
    ) -> None:
        """A later turn waits, re-reads persisted state, and becomes the final title."""
        session = mock_session_manager.get.return_value
        session.last_digest_input_hash = None
        session.last_digested_pair_index = 0
        first_call_started = asyncio.Event()
        release_first_call = asyncio.Event()

        async def generate_turn(_config, prompt: str, **_kwargs):
            if "First request" in prompt:
                first_call_started.set()
                await release_first_call.wait()
                return _turn_record_payload("Completed the first request.", "First Digest Title")
            return _turn_record_payload("Completed the second request.", "Second Digest Title")

        llm_service = MagicMock()
        llm_service.call_json_feature = AsyncMock(side_effect=generate_turn)

        def persist_state(_session_id: str, **values):
            session.last_turn_markdown = values["last_turn_markdown"]
            session.digest_markdown = values["digest_markdown"]
            session.last_digest_input_hash = values["last_digest_input_hash"]
            session.last_digested_pair_index = values["last_digested_pair_index"]
            if values["title"] is not None:
                session.title = values["title"]
                session.title_source = values["title_source"]
            return session

        mock_session_manager.persist_digest_state.side_effect = persist_state

        first = asyncio.create_task(
            build_turn_and_digest(
                memory_manager=mock_memory_manager,
                session_manager=mock_session_manager,
                session_id="session-123",
                prompt_text="First request",
                llm_service=llm_service,
                config=_digest_config(),
            )
        )
        await first_call_started.wait()
        second = asyncio.create_task(
            build_turn_and_digest(
                memory_manager=mock_memory_manager,
                session_manager=mock_session_manager,
                session_id="session-123",
                prompt_text="Second request",
                llm_service=llm_service,
                config=_digest_config(),
            )
        )

        assert llm_service.call_json_feature.await_count == 1

        release_first_call.set()
        first_result, second_result = await asyncio.gather(first, second)

        assert first_result is not None
        assert second_result is not None
        assert first_result["turn_num"] == 1
        assert second_result["turn_num"] == 2
        assert session.title == "Second Digest Title"
        assert session.title_source == "llm"
        assert "<!-- gobby:digest-turn:1 -->" in session.digest_markdown
        assert "<!-- gobby:digest-turn:2 -->" in session.digest_markdown

    @pytest.mark.asyncio
    async def test_digest_persistence_does_not_block_event_loop(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Synchronous digest persistence runs outside the async event loop."""
        started = threading.Event()
        release = threading.Event()
        persisted_session = mock_session_manager.persist_digest_state.return_value

        def blocking_persist(*args, **kwargs):
            started.set()
            release.wait(timeout=1.0)
            return persisted_session

        mock_session_manager.persist_digest_state.side_effect = blocking_persist

        result = await _assert_event_loop_progresses(
            build_turn_and_digest(
                memory_manager=mock_memory_manager,
                session_manager=mock_session_manager,
                session_id="session-123",
                prompt_text="Fix the authentication bug in auth.py",
                llm_service=mock_llm_service,
                config=_digest_config(),
            ),
            started,
            release,
        )

        assert result is not None
        mock_session_manager.persist_digest_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_provider_shutdown_cancellation_returns_cancelled_without_error_log(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
        caplog,
    ):
        mock_llm_service.call_json_feature.side_effect = LLMProviderCancellation(
            "generate_text[memory.turn_record] cancelled: provider exited [exit_code=143]"
        )

        with caplog.at_level(logging.INFO):
            result = await build_turn_and_digest(
                memory_manager=mock_memory_manager,
                session_manager=mock_session_manager,
                session_id="session-123",
                prompt_text="Summarize this turn",
                llm_service=mock_llm_service,
                config=_digest_config(),
            )

        assert result is not None
        assert result["cancelled"] is True
        assert "exit_code=143" in result["reason"]
        mock_session_manager.persist_digest_state.assert_not_called()
        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]

    @pytest.mark.asyncio
    async def test_cancellation_leaves_title_unchanged(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Provider cancellation preserves the provisional title for a later retry."""
        session = mock_session_manager.get.return_value
        session.title = "#42 Claude"
        session.title_source = "provisional"
        session.transcript_path = str(_CLAUDE_FIXTURE)
        session.source = "claude"

        mock_llm_service.call_json_feature.side_effect = LLMProviderCancellation(
            "generate_text[memory.turn_record] cancelled: provider exited [exit_code=143]"
        )

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Summarize this turn",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert result["cancelled"] is True
        assert "title" not in result
        mock_session_manager.update_title.assert_not_called()
        mock_session_manager.persist_digest_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_digest_persistence_failure_raises_without_legacy_writes(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Digest persistence failures surface without falling back to legacy writes."""
        mock_session_manager.persist_digest_state.return_value = None

        with pytest.raises(RuntimeError, match="Failed to persist session digest state"):
            await build_turn_and_digest(
                memory_manager=mock_memory_manager,
                session_manager=mock_session_manager,
                session_id="session-123",
                prompt_text="Fix the authentication bug in auth.py",
                llm_service=mock_llm_service,
                config=_digest_config(),
            )

        mock_session_manager.persist_digest_state.assert_called_once()
        assert mock_session_manager.persist_digest_state.call_args.kwargs["title"] == "Fix Auth Bug"
        mock_session_manager.update_title.assert_not_called()
        mock_session_manager.update_last_turn_markdown.assert_not_called()
        mock_session_manager.update_digest_markdown.assert_not_called()
        mock_session_manager.update_last_digest_input_hash.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_turn_record_contract_retries_then_returns_error_without_persistence(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Invalid turn-record fields fail after retries without persisting digest state."""
        mock_llm_service.call_json_feature = AsyncMock(
            return_value={"turn_markdown": "", "title_candidate": "Digest JSON Titles"}
        )

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the authentication bug in auth.py",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert "invalid JSON contract" in result["error"]
        assert mock_llm_service.call_json_feature.await_count == 3
        mock_session_manager.persist_digest_state.assert_not_called()
        mock_session_manager.update_last_turn_markdown.assert_not_called()
        mock_session_manager.update_digest_markdown.assert_not_called()
        mock_session_manager.update_title.assert_not_called()
        mock_session_manager.update_last_digest_input_hash.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_turn_record_contract_retries_and_recovers(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Transient turn-record contract failures retry and persist the valid response."""
        mock_llm_service.call_json_feature = AsyncMock(
            side_effect=[
                {"turn_markdown": "", "title_candidate": "Digest JSON Titles"},
                {"turn_markdown": "User asked for a fix."},
                _turn_record_payload(
                    "User asked to fix a bug. Agent retried malformed digest output.",
                    "Retry Digest JSON",
                ),
            ]
        )

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the authentication bug in auth.py",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert "error" not in result
        assert result["title"] == "Retry Digest JSON"
        assert mock_llm_service.call_json_feature.await_count == 3
        calls = mock_llm_service.call_json_feature.await_args_list
        assert "## Correction" not in calls[0].args[1]
        assert "## Correction" in calls[1].args[1]
        assert "## Correction" in calls[2].args[1]
        mock_session_manager.persist_digest_state.assert_called_once()
        assert (
            mock_session_manager.persist_digest_state.call_args.kwargs["title"]
            == "Retry Digest JSON"
        )

    @pytest.mark.asyncio
    async def test_contract_failure_logs_single_warning_per_retry(
        self,
        mock_memory_manager: MagicMock,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One retried contract failure emits a single WARNING, not a duplicate pair."""
        mock_llm_service.call_json_feature = AsyncMock(
            side_effect=[
                {"turn_markdown": "", "title_candidate": "Recovered"},
                _turn_record_payload("User asked for a fix. Agent recovered.", "Recovered"),
            ]
        )

        with caplog.at_level(logging.WARNING, logger="gobby.memory.digest"):
            result = await build_turn_and_digest(
                memory_manager=mock_memory_manager,
                session_manager=mock_session_manager,
                session_id="session-123",
                prompt_text="Fix the authentication bug in auth.py",
                llm_service=mock_llm_service,
                config=_digest_config(),
            )

        assert "error" not in result
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert sum("retrying after contract failure" in r.getMessage() for r in warnings) == 1
        assert not any("contract failed" in r.getMessage() for r in warnings)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "response",
        [
            {"title_candidate": "Digest JSON Titles"},
            {"turn_markdown": "", "title_candidate": "Digest JSON Titles"},
        ],
    )
    async def test_missing_or_empty_turn_markdown_returns_error_without_persistence(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
        response,
    ):
        """Missing or empty turn_markdown fails without persisting digest state."""
        mock_llm_service.call_json_feature = AsyncMock(return_value=response)

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the authentication bug in auth.py",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert "turn_markdown" in result["error"]
        mock_session_manager.persist_digest_state.assert_not_called()
        mock_session_manager.update_last_turn_markdown.assert_not_called()
        mock_session_manager.update_digest_markdown.assert_not_called()
        mock_session_manager.update_title.assert_not_called()
        mock_session_manager.update_last_digest_input_hash.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_title_candidate_fails_without_persisting_digest(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Missing title_candidate fails the strict turn-record contract."""
        mock_llm_service.call_json_feature = AsyncMock(
            return_value={"turn_markdown": "Did the work"}
        )

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the authentication bug in auth.py",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert "title_candidate" in result["error"]
        mock_session_manager.persist_digest_state.assert_not_called()
        mock_session_manager.update_last_turn_markdown.assert_not_called()
        mock_session_manager.update_digest_markdown.assert_not_called()
        mock_session_manager.update_title.assert_not_called()
        mock_session_manager.update_last_digest_input_hash.assert_not_called()

    @pytest.mark.asyncio
    async def test_placeholder_title_candidate_fails_without_persisting_digest(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Template-placeholder titles fail the strict turn-record contract."""
        mock_llm_service.call_json_feature = AsyncMock(
            return_value=_turn_record_payload(
                turn_markdown="User asked why a session title regressed.",
                title_candidate="[3-5 word session title]",
            )
        )

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Why did the title become a placeholder?",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert "title_candidate" in result["error"]
        mock_session_manager.persist_digest_state.assert_not_called()
        mock_session_manager.update_last_turn_markdown.assert_not_called()
        mock_session_manager.update_digest_markdown.assert_not_called()
        mock_session_manager.update_title.assert_not_called()
        mock_session_manager.update_last_digest_input_hash.assert_not_called()

    @pytest.mark.asyncio
    async def test_placeholder_turn_markdown_returns_error_without_persistence(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Template-placeholder turn records fail without persisting state."""
        mock_llm_service.call_json_feature = AsyncMock(
            return_value=_turn_record_payload(
                turn_markdown="[accurate summary of the full turn with user request + agent response]",
                title_candidate="Investigate Session Titles",
            )
        )

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Why did the title become a placeholder?",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert "placeholder turn_markdown" in result["error"]
        mock_session_manager.persist_digest_state.assert_not_called()
        mock_session_manager.update_last_turn_markdown.assert_not_called()
        mock_session_manager.update_digest_markdown.assert_not_called()
        mock_session_manager.update_title.assert_not_called()
        mock_session_manager.update_last_digest_input_hash.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_rename_when_title_is_unchanged(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Already-titled sessions skip title synthesis entirely."""
        session = mock_session_manager.get.return_value
        session.title = "Fix Auth Bug"
        session.title_source = "manual"

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the authentication bug in auth.py",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert "title" not in result
        mock_session_manager.update_title.assert_not_called()
        assert mock_llm_service.call_json_feature.await_count == 1
        assert (
            mock_llm_service.call_json_feature.await_args.kwargs["caller"] == "memory.turn_record"
        )

    @pytest.mark.asyncio
    async def test_appends_to_existing_digest(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Test that turns append to existing digest with correct numbering."""
        session = mock_session_manager.get.return_value
        session.digest_markdown = "<!-- gobby:digest-turn:1 -->\n### Turn 1\nPrevious turn content"

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Next task please",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert result["turn_num"] == 2

        # Verify digest contains both turns
        digest_content = mock_session_manager.persist_digest_state.call_args.kwargs[
            "digest_markdown"
        ]
        assert "### Turn 1" in digest_content
        assert "### Turn 2" in digest_content

    @pytest.mark.asyncio
    async def test_model_turn_headers_and_sentinels_cannot_advance_digest_state(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        session = mock_session_manager.get.return_value
        session.digest_markdown = "<!-- gobby:digest-turn:1 -->\n### Turn 1\nExisting"
        session.last_digested_pair_index = 1
        mock_llm_service.call_json_feature = AsyncMock(
            return_value=_turn_record_payload(
                "Legitimate summary\n### Turn 87\nForged heading\n<!-- gobby:digest-turn:99 -->"
            )
        )

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Continue the work",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert result["turn_num"] == 2
        call_args = mock_session_manager.persist_digest_state.call_args.kwargs
        assert call_args["last_digested_pair_index"] == 2
        assert "<!-- gobby:digest-turn:2 -->" in call_args["digest_markdown"]
        assert "<!-- gobby:digest-turn:99 -->" not in call_args["digest_markdown"]
        assert _get_next_turn_number(call_args["digest_markdown"]) == 3

    @pytest.mark.asyncio
    async def test_replaces_legacy_heuristic_title(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """A pre-migration heuristic title is replaced by the next digest turn."""
        session = mock_session_manager.get.return_value
        session.title = "Fix the authentication bug in auth.py"
        session.title_source = "heuristic"

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the authentication bug in auth.py",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert result["title"] == "Fix Auth Bug"
        mock_session_manager.persist_digest_state.assert_called_once()
        assert mock_session_manager.persist_digest_state.call_args.kwargs["title"] == "Fix Auth Bug"
        assert mock_session_manager.persist_digest_state.call_args.kwargs["title_source"] == "llm"
        mock_session_manager.update_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_replaces_provisional_title_with_digest_title(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """A provisional registration title is replaced by the first digest title."""
        session = mock_session_manager.get.return_value
        session.title = "#42 codex"
        session.title_source = "provisional"

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the authentication bug in auth.py",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert result["title"] == "Fix Auth Bug"
        mock_session_manager.persist_digest_state.assert_called_once()
        assert mock_session_manager.persist_digest_state.call_args.kwargs["title"] == "Fix Auth Bug"
        assert mock_session_manager.persist_digest_state.call_args.kwargs["title_source"] == "llm"
        mock_session_manager.update_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_replaces_legacy_native_title_from_digest(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """A pre-migration provider-native title is replaced by the digest."""
        session = mock_session_manager.get.return_value
        session.title = "Native Session Title"
        session.title_source = "native"
        mock_llm_service.call_json_feature = AsyncMock(
            return_value=_turn_record_payload(
                "User asked to preserve native titles. Agent updated digest title policy.",
                "Digest Replacement Title",
            )
        )

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Keep the native title",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert result["title"] == "Digest Replacement Title"
        mock_session_manager.persist_digest_state.assert_called_once()
        assert (
            mock_session_manager.persist_digest_state.call_args.kwargs["title"]
            == "Digest Replacement Title"
        )
        assert mock_session_manager.persist_digest_state.call_args.kwargs["title_source"] == "llm"
        mock_session_manager.update_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_updates_existing_llm_title_from_each_digest_turn(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """LLM-owned titles keep rolling with the digest."""
        session = mock_session_manager.get.return_value
        session.title = "Old Session Title"
        session.title_source = "llm"
        mock_llm_service.call_json_feature = AsyncMock(
            return_value=_turn_record_payload(
                "User asked for title updates. Agent implemented rolling titles.",
                "Rolling Digest Titles",
            )
        )

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Keep the title rolling",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert result["title"] == "Rolling Digest Titles"
        mock_session_manager.persist_digest_state.assert_called_once()
        assert (
            mock_session_manager.persist_digest_state.call_args.kwargs["title"]
            == "Rolling Digest Titles"
        )
        assert mock_session_manager.persist_digest_state.call_args.kwargs["title_source"] == "llm"
        mock_session_manager.update_title.assert_not_called()
        assert mock_llm_service.call_json_feature.await_count == 1

    @pytest.mark.asyncio
    async def test_replaces_non_empty_legacy_unknown_title(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """A null-source legacy title is automatic and replaced by the digest."""
        session = mock_session_manager.get.return_value
        session.title = "Legacy Title"
        session.title_source = None

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Keep the legacy title",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert result["title"] == "Fix Auth Bug"
        assert mock_session_manager.persist_digest_state.call_args.kwargs["title"] == "Fix Auth Bug"
        assert mock_session_manager.persist_digest_state.call_args.kwargs["title_source"] == "llm"
        mock_session_manager.update_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_digest_config_disabled(
        self, mock_memory_manager, mock_session_manager, mock_llm_service
    ):
        """Test that pipeline respects DigestConfig.enabled = False."""
        config = MagicMock()
        config.digest.enabled = False

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="s1",
            prompt_text="Some prompt",
            llm_service=mock_llm_service,
            config=config,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_reads_from_transcript_when_no_prompt(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
        tmp_path,
    ):
        """Test that transcript is read when prompt_text is None (stop event)."""
        import json

        jsonl_file = tmp_path / "transcript.jsonl"
        turns = [
            {"message": {"role": "user", "content": "Implement the feature"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Done. I implemented it."}],
                }
            },
        ]
        jsonl_file.write_text("\n".join(json.dumps(t) for t in turns))

        session = mock_session_manager.get.return_value
        session.transcript_path = str(jsonl_file)

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text=None,  # Simulates stop event
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert result["turn_num"] == 1
        # Verify the LLM was called with transcript content
        call_args = mock_llm_service.call_json_feature.call_args_list[0]
        prompt = call_args.args[1]
        assert "Implement the feature" in prompt or "feature" in prompt.lower()
        assert call_args.kwargs["caller"] == "memory.turn_record"


class TestBuildTurnAndDigestIdempotency:
    """Tests for digest idempotency via last_digest_input_hash."""

    @pytest.fixture
    def mock_memory_manager(self):
        mm = MagicMock()
        mm.config.enabled = True
        mm.content_exists.return_value = False
        mock_memory = MagicMock()
        mock_memory.id = "mem-123"
        mm.create_memory = AsyncMock(return_value=mock_memory)
        return mm

    @pytest.fixture
    def mock_session_manager(self):
        sm = MagicMock()
        session = MagicMock()
        session.id = "session-123"
        session.transcript_path = None
        session.source = "claude"
        session.digest_markdown = None
        session.summary_digest_turn_count = 5
        session.title = None
        session.title_source = None
        session.seq_num = 42
        session.terminal_context = None
        session.last_digest_input_hash = None  # No prior digest
        sm.get.return_value = session
        sm.update_last_turn_markdown.return_value = session
        sm.update_digest_markdown.return_value = session
        sm.update_title.return_value = session
        sm.update_last_digest_input_hash.return_value = None
        sm.persist_digest_state.return_value = session
        return sm

    @pytest.fixture
    def mock_llm_service(self):
        service = MagicMock()
        service.call_json_feature = AsyncMock(
            side_effect=[
                _turn_record_payload("User asked to fix a bug. Agent found the root cause."),
            ]
        )
        return service

    @pytest.mark.asyncio
    async def test_first_call_processes_and_stores_hash(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """First call should process normally and store the input hash."""
        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the bug",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert result["turn_num"] == 1
        # Hash should have been persisted with the rest of the digest state.
        mock_session_manager.persist_digest_state.assert_called_once()
        stored_hash = mock_session_manager.persist_digest_state.call_args.kwargs[
            "last_digest_input_hash"
        ]
        assert len(stored_hash) == 16  # sha256 hex truncated to 16 chars

    @pytest.mark.asyncio
    async def test_duplicate_content_skips(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Second call with same content should skip (return None)."""
        import hashlib

        prompt = "Fix the bug"
        response = ""  # No transcript, no response
        expected_hash = hashlib.sha256(f"0||{prompt}||{response}".encode()).hexdigest()[:16]

        # Simulate that the hash was already stored from a previous call
        session = mock_session_manager.get.return_value
        session.last_digest_input_hash = expected_hash

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text=prompt,
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is None
        mock_llm_service.call_json_feature.assert_not_called()
        mock_llm_service.call_feature.assert_not_called()

    @pytest.mark.asyncio
    async def test_different_content_processes(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Third call with different content should process normally."""
        import hashlib

        # Set hash from a previous different prompt
        old_hash = hashlib.sha256(b"old prompt||old response").hexdigest()[:16]
        session = mock_session_manager.get.return_value
        session.last_digest_input_hash = old_hash

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Now add tests for the fix",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert result["turn_num"] == 1
        # New hash should have been stored with the rest of the digest state.
        mock_session_manager.persist_digest_state.assert_called_once()
        new_hash = mock_session_manager.persist_digest_state.call_args.kwargs[
            "last_digest_input_hash"
        ]
        assert new_hash != old_hash


class TestReadUndigestedTurns:
    """Tests for _read_undigested_turns function."""

    def _write_claude_transcript(
        self, path: Path, exchanges: "Sequence[tuple[str, str | None]]"
    ) -> None:
        """Write a Claude-format JSONL transcript with given exchanges.

        Each exchange is a (user_text, assistant_text) tuple.
        If assistant_text is None, only the user turn is written (interrupted).
        """
        import json

        with open(path, "w") as f:
            for user_text, assistant_text in exchanges:
                user_turn = {
                    "type": "user",
                    "message": {"role": "user", "content": user_text},
                }
                f.write(json.dumps(user_turn) + "\n")
                if assistant_text is not None:
                    assistant_turn = {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": assistant_text}],
                        },
                    }
                    f.write(json.dumps(assistant_turn) + "\n")

    @pytest.mark.asyncio
    async def test_nonexistent_file(self) -> None:
        """Returns empty list for missing transcript."""
        batch = await _read_undigested_turns("/nonexistent/path.jsonl", "claude", 0)
        result = batch.pairs
        next_index = batch.next_pair_index
        assert result == []
        assert next_index == 0

    @pytest.mark.parametrize("source", [None, "", "unknown-cli"])
    async def test_unsupported_source_skips_with_log(
        self,
        source: str | None,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(transcript, [("prompt", "response")])

        with caplog.at_level(logging.WARNING):
            batch = await _read_undigested_turns(str(transcript), source, 0)
        result = batch.pairs
        next_index = batch.next_pair_index

        assert result == []
        assert next_index == 0
        assert "Skipping transcript" in caplog.text

    @pytest.mark.asyncio
    async def test_single_pair_backward_compat(self, tmp_path) -> None:
        """With 1 pair and 0 digested, returns that single pair."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(transcript, [("Hello", "Hi there")])

        batch = await _read_undigested_turns(str(transcript), "claude", 0)
        result = batch.pairs
        next_index = batch.next_pair_index
        assert len(result) == 1
        assert result[0][0] == "Hello"
        assert result[0][1] == "Hi there"
        assert next_index == 1

    @pytest.mark.asyncio
    async def test_catches_missed_turns(self, tmp_path) -> None:
        """With 3 pairs and 1 digested, returns 2 undigested."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [
                ("First question", "First answer"),
                ("Second question", "Second answer"),
                ("Third question", "Third answer"),
            ],
        )

        batch = await _read_undigested_turns(str(transcript), "claude", 1)
        result = batch.pairs
        next_index = batch.next_pair_index
        assert len(result) == 2
        assert result[0][0] == "Second question"
        assert result[0][1] == "Second answer"
        assert result[1][0] == "Third question"
        assert result[1][1] == "Third answer"
        assert next_index == 3

    @pytest.mark.asyncio
    async def test_cursor_beyond_fifty_pairs_reads_full_segment(self, tmp_path) -> None:
        transcript = tmp_path / "transcript.jsonl"
        exchanges = [(f"Question {number}", f"Answer {number}") for number in range(1, 52)]
        self._write_claude_transcript(transcript, exchanges)

        batch = await _read_undigested_turns(str(transcript), "claude", 50, num_pairs=50)
        result = batch.pairs
        next_index = batch.next_pair_index

        assert result == [("Question 51", "Answer 51")]
        assert next_index == 51

    @pytest.mark.asyncio
    async def test_lifecycle_commands_filtered(self, tmp_path) -> None:
        """Lifecycle commands like /clear are excluded from pairs."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [
                ("Real question", "Real answer"),
                ("/compact", "Compacted"),
                ("Another question", "Another answer"),
            ],
        )

        batch = await _read_undigested_turns(str(transcript), "claude", 0)
        result = batch.pairs
        assert len(result) == 2
        assert result[0][0] == "Real question"
        assert result[1][0] == "Another question"

    @pytest.mark.asyncio
    async def test_clear_boundary(self, tmp_path) -> None:
        """Only reads post-/clear content."""
        transcript = tmp_path / "transcript.jsonl"
        import json

        with open(transcript, "w") as f:
            # Pre-clear exchange
            f.write(
                json.dumps({"type": "user", "message": {"role": "user", "content": "Old question"}})
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Old answer"}],
                        },
                    }
                )
                + "\n"
            )
            # /clear boundary
            f.write(
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": (
                                "<command-name>/clear</command-name>\n"
                                "<command-message>clear</command-message>"
                            ),
                        },
                    }
                )
                + "\n"
            )
            # Post-clear exchange
            f.write(
                json.dumps({"type": "user", "message": {"role": "user", "content": "New question"}})
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "New answer"}],
                        },
                    }
                )
                + "\n"
            )

        batch = await _read_undigested_turns(str(transcript), "claude", 0)
        result = batch.pairs
        assert len(result) == 1
        assert result[0][0] == "New question"
        assert result[0][1] == "New answer"

    @pytest.mark.asyncio
    async def test_clear_boundary_advances_absolute_pair_cursor(self, tmp_path) -> None:
        import json

        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(transcript, [("Old question", "Old answer")])
        with open(transcript, "a") as transcript_file:
            records = [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": (
                            "<command-name>/clear</command-name>\n"
                            "<command-message>clear</command-message>"
                        ),
                    },
                },
                {
                    "type": "user",
                    "message": {"role": "user", "content": "New question"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "New answer"}],
                    },
                },
            ]
            for record in records:
                transcript_file.write(json.dumps(record) + "\n")

        batch = await _read_undigested_turns(str(transcript), "claude", 1)
        result = batch.pairs
        next_index = batch.next_pair_index
        repeated_batch = await _read_undigested_turns(str(transcript), "claude", next_index)
        repeated = repeated_batch.pairs
        repeated_index = repeated_batch.next_pair_index
        with open(transcript, "a") as transcript_file:
            for record in (
                {
                    "type": "user",
                    "message": {"role": "user", "content": "Later question"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Later answer"}],
                    },
                },
            ):
                transcript_file.write(json.dumps(record) + "\n")
        later_batch = await _read_undigested_turns(str(transcript), "claude", repeated_index)
        later = later_batch.pairs
        later_index = later_batch.next_pair_index

        assert result == [("New question", "New answer")]
        assert next_index == 2
        assert repeated == []
        assert repeated_index == 2
        assert later == [("Later question", "Later answer")]
        assert later_index == 3

    async def test_clear_cursor_survives_claude_segment_sanitization(self, tmp_path) -> None:
        import json

        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(transcript, [("Old question", "Old answer")])
        with open(transcript, "a") as transcript_file:
            for record in (
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": (
                            "<command-name>/clear</command-name>\n"
                            "<command-message>clear</command-message>"
                        ),
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "missing-tool-use",
                                "content": "orphaned result",
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "message": {"role": "user", "content": "New question"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "New answer"}],
                    },
                },
            ):
                transcript_file.write(json.dumps(record) + "\n")

        batch = await _read_undigested_turns(str(transcript), "claude", 1)
        result = batch.pairs
        next_index = batch.next_pair_index
        repeated_batch = await _read_undigested_turns(str(transcript), "claude", next_index)
        repeated = repeated_batch.pairs
        repeated_index = repeated_batch.next_pair_index

        assert result == [("New question", "New answer")]
        assert next_index == 2
        assert repeated == []
        assert repeated_index == 2

    @pytest.mark.asyncio
    async def test_interrupted_turn_pairs_with_empty_response(self, tmp_path) -> None:
        """An interrupted turn (user without assistant) gets empty response."""
        transcript = tmp_path / "transcript.jsonl"
        import json

        with open(transcript, "w") as f:
            # First user message (interrupted - no assistant response)
            f.write(
                json.dumps(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "Interrupted question"},
                    }
                )
                + "\n"
            )
            # Second user message (new message after interrupt)
            f.write(
                json.dumps(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "Follow-up question"},
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Final answer"}],
                        },
                    }
                )
                + "\n"
            )

        batch = await _read_undigested_turns(str(transcript), "claude", 0)
        result = batch.pairs
        assert len(result) == 2
        assert result[0] == ("Interrupted question", "")
        assert result[1] == ("Follow-up question", "Final answer")

    @pytest.mark.asyncio
    async def test_codex_catch_up_excludes_active_turn(self, tmp_path: Path) -> None:
        transcript = tmp_path / "codex.jsonl"
        _write_interrupted_codex_transcript(transcript)

        batch = await _read_undigested_turns(
            str(transcript),
            "codex",
            0,
            catch_up=True,
        )
        result = batch.pairs
        next_index = batch.next_pair_index
        repeated_batch = await _read_undigested_turns(
            str(transcript),
            "codex",
            next_index,
            catch_up=True,
        )
        repeated = repeated_batch.pairs
        repeated_index = repeated_batch.next_pair_index

        assert result == [("Investigate the title", "The title is still provisional.")]
        assert next_index == 1
        assert repeated == []
        assert repeated_index == next_index

    @pytest.mark.asyncio
    async def test_claude_catch_up_drains_backlog_and_excludes_active_prompt(
        self, tmp_path: Path
    ) -> None:
        """Non-codex catch-up drains completed pairs in bounded batches."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [("Q1", "A1"), ("Q2", "A2"), ("Q3", "A3"), ("Active prompt", None)],
        )

        first_batch = await _read_undigested_turns(
            str(transcript), "claude", 0, num_pairs=2, catch_up=True
        )
        first = first_batch.pairs
        first_index = first_batch.next_pair_index
        second_batch = await _read_undigested_turns(
            str(transcript), "claude", first_index, num_pairs=2, catch_up=True
        )
        second = second_batch.pairs
        second_index = second_batch.next_pair_index
        drained_batch = await _read_undigested_turns(
            str(transcript), "claude", second_index, num_pairs=2, catch_up=True
        )
        drained = drained_batch.pairs
        drained_index = drained_batch.next_pair_index

        assert first == [("Q1", "A1"), ("Q2", "A2")]
        assert first_index == 2
        assert second == [("Q3", "A3")]
        assert second_index == 3
        assert drained == []
        assert drained_index == 3

    @pytest.mark.asyncio
    async def test_daemon_wake_pairs_without_response_are_skipped(self, tmp_path: Path) -> None:
        """Daemon pings with no agent response never become digest pairs."""
        transcript = tmp_path / "transcript.jsonl"
        wake = "Message from Gobby daemon: New activity available."
        self._write_claude_transcript(
            transcript,
            [
                (wake, None),
                ("Real question", "Real answer"),
                (wake, "Handled the new activity."),
            ],
        )

        batch = await _read_undigested_turns(str(transcript), "claude", 0)
        result = batch.pairs
        next_index = batch.next_pair_index

        assert result == [
            ("Real question", "Real answer"),
            (wake, "Handled the new activity."),
        ]
        assert next_index == 2

    @pytest.mark.asyncio
    async def test_hook_blocking_attachment_does_not_create_digest_exchange(self, tmp_path) -> None:
        """A captured Claude hook-block attachment is excluded from digest exchanges."""
        transcript = tmp_path / "transcript.jsonl"
        import json

        fixture = (
            Path(__file__).parents[1]
            / "fixtures"
            / "transcripts"
            / "claude-hook-blocking-error.jsonl"
        )
        hook_block = json.loads(fixture.read_text())

        with open(transcript, "w") as f:
            for turn in (
                {
                    "type": "user",
                    "message": {"role": "user", "content": "Run the command"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_blocked",
                                "name": "Bash",
                                "input": {"command": "python script.py"},
                            }
                        ],
                    },
                },
                hook_block,
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "I will use uv instead."}],
                    },
                },
            ):
                f.write(json.dumps(turn) + "\n")

        batch = await _read_undigested_turns(str(transcript), "claude", 0)
        result = batch.pairs

        assert len(result) == 1
        assert result[0][0] == "Run the command"
        assert result[0][1].startswith("I will use uv instead.\n\n[tool activity]\n")
        assert "- Bash python script.py (no result recorded)" in result[0][1]

    @pytest.mark.asyncio
    async def test_cursor_past_new_segment_resets_to_segment_start(self, tmp_path) -> None:
        """A /clear-style segment reset consumes the whole replacement segment."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [("Q1", "A1"), ("Q2", "A2")],
        )

        batch = await _read_undigested_turns(str(transcript), "claude", 5)
        result = batch.pairs
        next_index = batch.next_pair_index
        assert result == [("Q1", "A1"), ("Q2", "A2")]
        assert next_index == 2

    @pytest.mark.asyncio
    async def test_injected_handoff_block_is_stripped_and_empty_pair_dropped(
        self, tmp_path
    ) -> None:
        """Injected handoff-only transcript content is not emitted for digesting."""
        transcript = tmp_path / "transcript.jsonl"
        injected = (
            "## Previous Session Context\n"
            "*Injected by Gobby session handoff*\n\n"
            "/Users/josh/Projects/gobby/src/gobby/memory/recall.py"
        )
        self._write_claude_transcript(
            transcript,
            [
                (injected, ""),
                ("Real follow-up", "Real response"),
            ],
        )

        batch = await _read_undigested_turns(str(transcript), "claude", 0)
        result = batch.pairs

        assert result == [("Real follow-up", "Real response")]

    @pytest.mark.asyncio
    async def test_last_turn_strips_injected_context(self, tmp_path) -> None:
        """The single-turn reader strips injected context before summary input."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [
                (
                    "Question\n"
                    "<!-- gobby:injected-context:begin -->\n"
                    "injected\n"
                    "<!-- gobby:injected-context:end -->",
                    "Answer",
                )
            ],
        )

        result = await _read_last_turn_from_transcript(str(transcript), "claude")

        assert result == ("Question", "Answer")


class TestBuildTurnAndDigestCatchUp:
    """Tests for build_turn_and_digest catching up on missed turns."""

    @pytest.fixture
    def mock_memory_manager(self):
        mm = MagicMock()
        mm.config.enabled = True
        mm.content_exists.return_value = False
        mock_memory = MagicMock()
        mock_memory.id = "mem-456"
        mm.create_memory = AsyncMock(return_value=mock_memory)
        return mm

    @pytest.fixture
    def mock_llm_service(self):
        service = MagicMock()
        service.call_json_feature = AsyncMock(
            side_effect=[
                _turn_record_payload(
                    "User asked two questions. Agent answered both.",
                    "Multi-Exchange Session",
                ),
            ]
        )
        return service

    def _write_claude_transcript(
        self, path: Path, exchanges: "Sequence[tuple[str, str | None]]"
    ) -> None:
        """Write a Claude-format JSONL transcript."""
        import json

        with open(path, "w") as f:
            for user_text, assistant_text in exchanges:
                f.write(
                    json.dumps({"type": "user", "message": {"role": "user", "content": user_text}})
                    + "\n"
                )
                if assistant_text is not None:
                    f.write(
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": assistant_text}],
                                },
                            }
                        )
                        + "\n"
                    )

    @pytest.mark.asyncio
    async def test_catches_up_missed_turns(
        self,
        mock_memory_manager,
        mock_llm_service,
        tmp_path,
    ):
        """Session with 1 digested turn + 2 undigested: digest has Turn 2 covering both."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [
                ("First question", "First answer"),
                ("Second question", "Second answer"),
                ("Third question", "Third answer"),
            ],
        )

        sm = MagicMock()
        session = MagicMock()
        session.id = "session-456"
        session.transcript_path = str(transcript)
        session.source = "claude"
        session.digest_markdown = (
            "<!-- gobby:digest-turn:1 -->\n### Turn 1\nFirst turn already digested"
        )
        session.last_digested_pair_index = 1
        session.title = None
        session.title_source = None
        session.seq_num = 99
        session.terminal_context = None
        session.last_digest_input_hash = None
        sm.get.return_value = session
        sm.update_last_turn_markdown.return_value = session
        sm.update_digest_markdown.return_value = session
        sm.update_title.return_value = session
        sm.update_last_digest_input_hash.return_value = None
        sm.persist_digest_state.return_value = session

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=sm,
            session_id="session-456",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is not None
        assert result["turn_num"] == 2

        # Verify the LLM was called with multi-exchange content
        turn_prompt_call = mock_llm_service.call_json_feature.call_args_list[0]
        prompt_text = turn_prompt_call.args[1]
        assert "Exchange 1" in prompt_text
        assert "Exchange 2" in prompt_text
        assert "Second question" in prompt_text
        assert "Third question" in prompt_text
        assert turn_prompt_call.kwargs["caller"] == "memory.turn_record"

        # Verify digest contains both turns
        digest_content = sm.persist_digest_state.call_args.kwargs["digest_markdown"]
        assert "### Turn 1" in digest_content
        assert "### Turn 2" in digest_content
        persisted_index = sm.persist_digest_state.call_args.kwargs["last_digested_pair_index"]
        assert persisted_index == 3
        remaining_batch = await _read_undigested_turns(str(transcript), "claude", persisted_index)
        remaining = remaining_batch.pairs
        next_index = remaining_batch.next_pair_index
        assert remaining == []
        assert next_index == 3

    @pytest.mark.asyncio
    async def test_catch_up_consumes_bounded_backlog_batch(
        self,
        mock_memory_manager,
        mock_llm_service,
        tmp_path,
    ):
        """Catch-up drains at most catch_up_num_pairs per pass, not the whole backlog."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [(f"Question {number}", f"Answer {number}") for number in range(1, 9)],
        )

        sm = MagicMock()
        session = MagicMock()
        session.id = "session-456"
        session.transcript_path = str(transcript)
        session.source = "claude"
        session.digest_markdown = None
        session.last_digested_pair_index = 0
        session.title = None
        session.title_source = None
        session.seq_num = 99
        session.terminal_context = None
        session.last_digest_input_hash = None
        sm.get.return_value = session
        sm.persist_digest_state.return_value = session

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=sm,
            session_id="session-456",
            llm_service=mock_llm_service,
            config=_digest_config(),
            catch_up=True,
        )

        assert result is not None
        persisted_index = sm.persist_digest_state.call_args.kwargs["last_digested_pair_index"]
        assert persisted_index == 5
        prompt_text = mock_llm_service.call_json_feature.call_args_list[0].args[1]
        assert "Question 5" in prompt_text
        assert "Question 6" not in prompt_text

    @pytest.mark.asyncio
    async def test_session_past_fifty_pairs_processes_next_exchange(
        self,
        mock_memory_manager,
        mock_llm_service,
        tmp_path,
    ):
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [(f"Question {number}", f"Answer {number}") for number in range(1, 52)],
        )

        sm = MagicMock()
        session = MagicMock()
        session.id = "session-456"
        session.transcript_path = str(transcript)
        session.source = "claude"
        session.digest_markdown = (
            "<!-- gobby:digest-turn:50 -->\n### Turn 50\nEarlier exchanges digested"
        )
        session.last_digested_pair_index = 50
        session.last_digest_input_hash = None
        session.title = "Long Session"
        session.title_source = "manual"
        session.seq_num = 99
        session.terminal_context = None
        sm.get.return_value = session
        sm.persist_digest_state.return_value = session

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=sm,
            session_id="session-456",
            llm_service=mock_llm_service,
            config=_digest_config(num_pairs=50),
        )

        assert result is not None
        assert result["turn_num"] == 51
        prompt = mock_llm_service.call_json_feature.await_args.args[1]
        assert "Question 51" in prompt
        assert "Answer 51" in prompt
        assert sm.persist_digest_state.call_args.kwargs["last_digested_pair_index"] == 51

    @pytest.mark.asyncio
    async def test_idempotency_combined_hash(
        self,
        mock_memory_manager: MagicMock,
        mock_llm_service: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Same batch of undigested pairs doesn't re-process."""
        import hashlib

        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [
                ("First question", "First answer"),
                ("Second question", "Second answer"),
            ],
        )

        # Compute the expected hash for the 2 undigested pairs
        combined = "0||First question||First answer||Second question||Second answer"
        expected_hash = hashlib.sha256(combined.encode()).hexdigest()[:16]

        sm = MagicMock()
        session = MagicMock()
        session.id = "session-456"
        session.transcript_path = str(transcript)
        session.source = "claude"
        session.digest_markdown = None  # 0 digested
        session.last_digested_pair_index = 0
        session.seq_num = 99
        session.terminal_context = None
        session.last_digest_input_hash = expected_hash  # Already processed
        sm.get.return_value = session

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=sm,
            session_id="session-456",
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is None
        mock_llm_service.call_json_feature.assert_not_called()


def _digest_dependencies(
    *, transcript_path: str | None = "transcript.jsonl"
) -> tuple[
    MagicMock,
    MagicMock,
    MagicMock,
    MagicMock,
]:
    memory_manager = MagicMock()
    memory_manager.config.enabled = True
    session_manager = MagicMock()
    session = MagicMock()
    session.id = "session-tail"
    session.transcript_path = transcript_path
    session.source = "claude"
    session.digest_markdown = None
    session.summary_digest_turn_count = 0
    session.summary_markdown = "## Current State\nSnapshot"
    session.last_digested_pair_index = 0
    session.last_digest_input_hash = None
    session.title = None
    session.title_source = None
    session.seq_num = 1
    session.terminal_context = None
    session_manager.get.return_value = session
    session_manager.persist_digest_state.return_value = session
    llm_service = MagicMock()
    llm_service.call_json_feature = AsyncMock(
        return_value=_turn_record_payload("Recorded the complete prefix.", "Tail Handling")
    )
    return memory_manager, session_manager, session, llm_service


@pytest.mark.asyncio
async def test_tool_only_turn_ledger_stays_on_current_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = tmp_path / "tool-only.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")

    class ToolOnlyParser:
        def extract_turns_since_clear(
            self, turns: list[dict[str, Any]], max_turns: int | None = None
        ) -> list[dict[str, Any]]:
            return turns

        def extract_last_messages(
            self,
            turns: list[dict[str, Any]],
            num_pairs: int,
            *,
            include_tool_activity: bool = False,
        ) -> list[dict[str, Any]]:
            assert include_tool_activity is True
            return [
                {"role": "user", "content": "First prompt"},
                {"role": "assistant", "content": "First response"},
                {
                    "role": "user",
                    "content": "Run checks",
                    "tool_activity": "[tool activity]\n- Bash uv run pytest tests/unit -q",
                },
                {"role": "assistant", "content": ""},
            ]

    monkeypatch.setattr(
        "gobby.memory.digest._parser_for_transcript",
        lambda _source, _path: ToolOnlyParser(),
    )

    batch = await _read_undigested_turns(str(transcript), "claude", 1)
    catch_up = await _read_undigested_turns(str(transcript), "claude", 1, catch_up=True)

    assert batch.pairs == [("Run checks", "[tool activity]\n- Bash uv run pytest tests/unit -q")]
    assert batch.next_pair_index == 2
    assert batch.tail_pair == DigestPair(
        "Run checks",
        "",
        "[tool activity]\n- Bash uv run pytest tests/unit -q",
    )
    assert catch_up.pairs == []
    assert catch_up.next_pair_index == 1


@pytest.mark.asyncio
async def test_partial_transcript_tail_withholds_trailing_pair(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "Complete prompt"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Complete response"}],
            },
        },
        {"type": "user", "message": {"role": "user", "content": "Trailing prompt"}},
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )
    with transcript.open("ab") as stream:
        stream.write(b'{"type":"assistant"')

    batch = await _read_undigested_turns(str(transcript), "claude", 0)

    assert batch.pairs == [("Complete prompt", "Complete response")]
    assert batch.next_pair_index == 1
    assert batch.tail_withheld is True
    assert batch.tail_pair is not None
    assert batch.tail_pair.prompt == "Trailing prompt"


@pytest.mark.asyncio
async def test_completed_tail_record_reaches_ledger_after_withhold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = tmp_path / "completed-tail.jsonl"

    class CompletingParser:
        def extract_turns_since_clear(
            self, turns: list[dict[str, Any]], max_turns: int | None = None
        ) -> list[dict[str, Any]]:
            return turns

        def extract_last_messages(
            self,
            turns: list[dict[str, Any]],
            num_pairs: int,
            *,
            include_tool_activity: bool = False,
        ) -> list[dict[str, Any]]:
            complete = any(turn.get("complete") is True for turn in turns)
            suffix = "" if complete else " (no result recorded)"
            return [
                {"role": "user", "content": "First"},
                {"role": "assistant", "content": "Done"},
                {
                    "role": "user",
                    "content": "Run command",
                    "tool_activity": f"[tool activity]\n- Bash uv run pytest{suffix}",
                },
                {"role": "assistant", "content": ""},
            ]

    monkeypatch.setattr(
        "gobby.memory.digest._parser_for_transcript",
        lambda _source, _path: CompletingParser(),
    )
    transcript.write_bytes(b'{}\n{}\n{}\n{"complete"')

    withheld = await _read_undigested_turns(str(transcript), "claude", 0)
    transcript.write_text('{}\n{}\n{}\n{"complete": true}\n', encoding="utf-8")
    completed = await _read_undigested_turns(str(transcript), "claude", withheld.next_pair_index)

    assert withheld.next_pair_index == 1
    assert withheld.tail_withheld is True
    assert withheld.tail_pair is not None
    assert "(no result recorded)" in withheld.tail_pair.activity
    assert completed.next_pair_index == 2
    assert completed.tail_withheld is False
    assert completed.pairs == [("Run command", "[tool activity]\n- Bash uv run pytest")]


@pytest.mark.asyncio
async def test_tail_withheld_propagates_to_public_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_manager, session_manager, _session, llm_service = _digest_dependencies()
    withheld_pair = DigestPair(
        "Trailing prompt",
        "partial narration",
        "[tool activity]\n- Bash uv run pytest (no result recorded)",
    )
    read = AsyncMock(return_value=UndigestedBatch([], 0, True, withheld_pair))
    monkeypatch.setattr("gobby.memory.digest._read_undigested_turns", read)
    capture: dict[str, Any] = {}

    only_tail = await build_turn_and_digest(
        memory_manager,
        session_manager,
        "session-tail",
        llm_service=llm_service,
        config=_digest_config(),
        withheld_capture=capture,
    )

    assert only_tail == {"tail_withheld": True, "withheld_pair": withheld_pair._asdict()}
    assert capture == only_tail
    llm_service.call_json_feature.assert_not_awaited()
    session_manager.persist_digest_state.assert_not_called()

    read.return_value = UndigestedBatch(
        [("Complete prompt", "Complete response")],
        1,
        True,
        withheld_pair,
    )
    llm_service.call_json_feature.side_effect = RuntimeError("provider unavailable")
    failed = await build_turn_and_digest(
        memory_manager,
        session_manager,
        "session-tail",
        llm_service=llm_service,
        config=_digest_config(),
        withheld_capture=capture,
    )

    assert failed is not None
    assert failed["error"] == "provider unavailable"
    assert failed["tail_withheld"] is True
    assert failed["withheld_pair"] == withheld_pair._asdict()
    assert capture["withheld_pair"] == withheld_pair._asdict()

    complete_pair = DigestPair(
        "Complete prompt",
        "Complete response",
        "[tool activity]\n- Bash uv run pytest",
    )
    read.return_value = UndigestedBatch(
        [(complete_pair.prompt, f"{complete_pair.response}\n\n{complete_pair.activity}")],
        1,
        False,
        complete_pair,
    )
    completed_failure = await build_turn_and_digest(
        memory_manager,
        session_manager,
        "session-tail",
        llm_service=llm_service,
        config=_digest_config(),
        withheld_capture=capture,
    )

    assert completed_failure is not None
    assert "tail_withheld" not in completed_failure
    assert capture == {
        "tail_withheld": False,
        "withheld_pair": complete_pair._asdict(),
    }

    read.side_effect = RuntimeError("read failed before resolution")
    before_read_failure = dict(capture)
    read_failure = await build_turn_and_digest(
        memory_manager,
        session_manager,
        "session-tail",
        llm_service=llm_service,
        config=_digest_config(),
        withheld_capture=capture,
    )
    assert read_failure == {"error": "read failed before resolution"}
    assert capture == before_read_failure


@pytest.mark.asyncio
async def test_cancelled_digest_holds_lock_through_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_manager, session_manager, session, llm_service = _digest_dependencies()
    started = threading.Event()
    release = threading.Event()
    barrier_waiting = asyncio.Event()
    original_wait = asyncio.wait

    async def observed_wait(
        futures: set[asyncio.Future[Any]],
    ) -> tuple[set[asyncio.Future[Any]], set[asyncio.Future[Any]]]:
        barrier_waiting.set()
        return await original_wait(futures)

    async def read_batch(
        _path: str,
        _source: str,
        digested_pair_index: int,
        num_pairs: int = 50,
        *,
        catch_up: bool = False,
    ) -> UndigestedBatch:
        if digested_pair_index:
            return UndigestedBatch([], digested_pair_index, False, None)
        pair = DigestPair("Same prompt", "Same response", "")
        return UndigestedBatch([("Same prompt", "Same response")], 1, False, pair)

    monkeypatch.setattr("gobby.memory.digest._read_undigested_turns", read_batch)
    monkeypatch.setattr("gobby.memory.digest.asyncio.wait", observed_wait)

    def persist(_session_id: str, **values: Any) -> MagicMock:
        started.set()
        release.wait(timeout=2)
        session.last_digest_input_hash = values["last_digest_input_hash"]
        session.last_digested_pair_index = values["last_digested_pair_index"]
        session.digest_markdown = values["digest_markdown"]
        return session

    session_manager.persist_digest_state.side_effect = persist
    first = asyncio.create_task(
        build_turn_and_digest(
            memory_manager,
            session_manager,
            "session-tail",
            llm_service=llm_service,
            config=_digest_config(),
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    first.cancel()
    await asyncio.wait_for(barrier_waiting.wait(), timeout=1)
    second = asyncio.create_task(
        build_turn_and_digest(
            memory_manager,
            session_manager,
            "session-tail",
            llm_service=llm_service,
            config=_digest_config(),
        )
    )
    assert not first.done()
    assert not second.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await second is None
    assert session.last_digested_pair_index == 1
    assert llm_service.call_json_feature.await_count == 1

    session.last_digested_pair_index = 0
    session.last_digest_input_hash = None
    session.digest_markdown = None
    session_manager.persist_digest_state.reset_mock()
    llm_started = asyncio.Event()

    async def slow_llm(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        llm_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    llm_service.call_json_feature.side_effect = slow_llm
    cancelled_before_persist = asyncio.create_task(
        build_turn_and_digest(
            memory_manager,
            session_manager,
            "session-tail",
            llm_service=llm_service,
            config=_digest_config(),
        )
    )
    await asyncio.wait_for(llm_started.wait(), timeout=1)
    cancelled_before_persist.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_before_persist
    session_manager.persist_digest_state.assert_not_called()


@pytest.mark.asyncio
async def test_withheld_tail_suppresses_summary_refresh_scheduling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_manager, session_manager, session, llm_service = _digest_dependencies()
    tail = DigestPair("Tail", "", "[tool activity]\n- Read file.py")
    read = AsyncMock(
        side_effect=[
            UndigestedBatch([("Prefix", "Done")], 1, True, tail),
            UndigestedBatch([("Tail", tail.activity)], 2, False, tail),
        ]
    )
    monkeypatch.setattr("gobby.memory.digest._read_undigested_turns", read)
    config = _DigestTestConfig(digest=_digest_config().digest, session_summary=object())

    await build_turn_and_digest(
        memory_manager,
        session_manager,
        "session-tail",
        llm_service=llm_service,
        config=config,
    )
    first_names = [
        call.kwargs.get("name") for call in memory_manager.schedule_background_task.call_args_list
    ]
    assert "session-summary-refresh-session-tail" not in first_names

    session.last_digest_input_hash = None
    await build_turn_and_digest(
        memory_manager,
        session_manager,
        "session-tail",
        llm_service=llm_service,
        config=config,
    )
    all_names = [
        call.kwargs.get("name") for call in memory_manager.schedule_background_task.call_args_list
    ]
    assert "session-summary-refresh-session-tail" in all_names


@pytest.mark.asyncio
async def test_repeated_cancellation_holds_lock_until_persistence_settles(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_manager, session_manager, _session, llm_service = _digest_dependencies(
        transcript_path=None
    )
    started = threading.Event()
    release = threading.Event()
    barrier_waiting = asyncio.Event()
    original_wait = asyncio.wait

    async def observed_wait(
        futures: set[asyncio.Future[Any]],
    ) -> tuple[set[asyncio.Future[Any]], set[asyncio.Future[Any]]]:
        barrier_waiting.set()
        return await original_wait(futures)

    monkeypatch.setattr("gobby.memory.digest.asyncio.wait", observed_wait)

    def failing_persist(_session_id: str, **_values: Any) -> None:
        started.set()
        release.wait(timeout=2)
        raise RuntimeError("write failed")

    session_manager.persist_digest_state.side_effect = failing_persist
    task = asyncio.create_task(
        build_turn_and_digest(
            memory_manager,
            session_manager,
            "session-tail",
            prompt_text="Persist once",
            llm_service=llm_service,
            config=_digest_config(),
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.wait_for(barrier_waiting.wait(), timeout=1)
    task.cancel()
    assert not task.done()

    with caplog.at_level(logging.WARNING):
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert "digest persistence failed for session-tail during cancellation" in caplog.text
