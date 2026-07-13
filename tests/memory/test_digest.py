"""Tests for memory digest pipeline functions.

Relocated from tests/workflows/test_memory_actions.py as part of dead-code cleanup.
"""

import asyncio
import hashlib
import logging
import threading
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.sessions import DigestConfig
from gobby.llm.base import LLMProviderCancellation
from gobby.memory.digest import (
    _build_title_synthesis_prompt,
    _build_turn_record_prompt,
    _can_replace_with_native_title,
    _get_next_turn_number,
    _parse_turn_record_response,
    _read_last_turn_from_transcript,
    _read_undigested_turns,
    _should_update_digest_title,
    _synthesize_title,
    bootstrap_session_title,
    build_turn_and_digest,
    memory_sync_export,
    memory_sync_import,
)
from gobby.memory.title_heuristics import (
    build_heuristic_title,
    heuristic_title_from_transcript,
    is_template_placeholder,
    normalize_native_title,
    normalize_title_candidate,
)
from tests._timing import wait_forever


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


def _digest_config(**kwargs: object) -> _DigestTestConfig:
    return _DigestTestConfig(digest=DigestConfig(candidates=["claude/haiku"], **kwargs))


def _turn_record_json(
    turn_markdown: str = "User asked to fix a bug. Agent found the root cause.",
    title_candidate: str | None = "Fix Auth Bug",
) -> str:
    import json

    payload = {"turn_markdown": turn_markdown}
    if title_candidate is not None:
        payload["title_candidate"] = title_candidate
    return json.dumps(payload)


class TestMemorySyncImportDirect:
    """Direct tests for memory_sync_import function."""

    @pytest.mark.asyncio
    async def test_memory_sync_import_no_manager(self):
        """Test memory_sync_import returns error when manager is None."""
        result = await memory_sync_import(None)
        assert result == {"error": "Memory Sync Manager not available"}

    @pytest.mark.asyncio
    async def test_memory_sync_import_success(self):
        """Test memory_sync_import success path."""
        mock_manager = AsyncMock()
        mock_manager.import_from_files.return_value = 5

        result = await memory_sync_import(mock_manager)

        assert result == {"imported": {"memories": 5}}
        mock_manager.import_from_files.assert_awaited_once()


def test_turn_record_contract_log_omits_full_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response_text = "not json " + ("x" * 220) + " sensitive-tail"
    response_sha = hashlib.sha256(response_text.encode("utf-8")).hexdigest()

    with caplog.at_level(logging.DEBUG, logger="gobby.memory.digest"):
        with pytest.raises(ValueError, match="invalid JSON contract"):
            _parse_turn_record_response(response_text, exchange_count=1)

    assert response_sha in caplog.text
    assert f"response_chars={len(response_text)}" in caplog.text
    assert response_text[:40] in caplog.text
    assert "sensitive-tail" not in caplog.text


class TestMemorySyncExportDirect:
    """Direct tests for memory_sync_export function."""

    @pytest.mark.asyncio
    async def test_memory_sync_export_no_manager(self):
        """Test memory_sync_export returns error when manager is None."""
        result = await memory_sync_export(None)
        assert result == {"error": "Memory Sync Manager not available"}

    @pytest.mark.asyncio
    async def test_memory_sync_export_skips_outside_jsonl_export_context(self, monkeypatch):
        """Test memory_sync_export avoids local JSONL writes outside remote push."""
        monkeypatch.delenv("GOBBY_JSONL_EXPORT_CONTEXT", raising=False)
        mock_manager = AsyncMock()

        result = await memory_sync_export(mock_manager)

        assert result == {
            "exported": {"memories": 0},
            "skipped": True,
            "reason": "not_remote_push",
        }
        mock_manager.export_to_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_memory_sync_export_success(self, monkeypatch):
        """Test memory_sync_export success path."""
        monkeypatch.setenv("GOBBY_JSONL_EXPORT_CONTEXT", "pre-push")
        mock_manager = AsyncMock()
        mock_manager.export_to_files.return_value = 7

        result = await memory_sync_export(mock_manager)

        assert result == {"exported": {"memories": 7}}
        mock_manager.export_to_files.assert_awaited_once()


# =============================================================================
# MEMORY INJECT PROJECT CONTEXT TESTS
# =============================================================================


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


class TestBuildHeuristicTitle:
    """Tests for the first-prompt heuristic title bootstrap."""

    def test_returns_none_for_empty_or_command_prompt(self) -> None:
        assert build_heuristic_title("") is None
        assert build_heuristic_title("   ") is None
        assert build_heuristic_title("/clear") is None
        assert build_heuristic_title("/gobby plan") is None
        assert build_heuristic_title("/gobby coderabbit") is None
        assert build_heuristic_title("$gobby coderabbit") is None

    def test_returns_none_for_interrupt_control_marker(self) -> None:
        assert build_heuristic_title("[Request interrupted by user]") is None
        assert build_heuristic_title("[Request interrupted by user for tool use]") is None

    @pytest.mark.parametrize(
        "prompt",
        [
            "# AGENTS.md instructions for /repo\n\n<INSTRUCTIONS>\nProject rules.",
            "\n".join(
                [
                    "<permissions instructions>",
                    "Filesystem sandboxing defines which files can be read or written.",
                    "</permissions instructions>",
                    "<collaboration_mode>",
                    "Known mode names are Default and Plan.",
                    "</collaboration_mode>",
                    "Gobby Session ID: #7404 (8a2d79bf-3f79-48aa-b516-f4bb97e0892f)",
                    "",
                    "## Instructions",
                    "Use tools progressively.",
                ]
            ),
            "<turn_aborted>The user interrupted the previous turn.</turn_aborted>",
            "Message from Gobby daemon: New activity available.",
            (
                "Continue where you last left off. Before continuing, call "
                '`gobby-sessions.wait_for_summary(session_id="s1")`. If it returns '
                "`completed=false`, repeat the same wait call."
            ),
        ],
    )
    def test_returns_none_for_synthetic_bootstrap_prompts(self, prompt: str) -> None:
        assert build_heuristic_title(prompt) is None

    def test_rejects_orchestration_boilerplate_without_h1(self) -> None:
        assert (
            build_heuristic_title(
                "A previous agent produced the plan below to accomplish the user's task. "
                "Implement the plan in a fresh context."
            )
            is None
        )

    def test_uses_h1_from_orchestration_boilerplate(self) -> None:
        title = build_heuristic_title(
            "A previous agent produced the plan below to accomplish the user's task.\n\n"
            "# Atomic JSON Digest And Rolling Titles\n\n"
            "Make the existing digest LLM call return JSON."
        )
        assert title == "Atomic JSON Digest And Rolling Titles"

    def test_strips_leading_phrase_and_truncates(self) -> None:
        title = build_heuristic_title(
            "I'd like to generate a session title on the first user prompt submit."
        )
        assert title == "Generate a session title on the first"

    def test_handles_multimodal_blocks(self) -> None:
        title = build_heuristic_title(
            [
                {"type": "text", "text": "Fix the auth bug in login.py"},
                {"type": "image", "image_url": "ignored"},
            ]
        )
        assert title == "Fix the auth bug in login.py"

    def test_rejects_titles_that_truncate_to_too_short(self) -> None:
        assert build_heuristic_title("A") is None

    def test_allows_two_character_titles(self) -> None:
        assert build_heuristic_title("PR") == "PR"

    def test_strips_gobby_namespace_and_subcommand(self) -> None:
        title = build_heuristic_title(
            "/gobby plan why aren't tmux titles updating in claude sessions"
        )
        assert title == "Why aren't tmux titles updating in claude"

        assert (
            build_heuristic_title("$gobby coderabbit fix review comments") == "Fix review comments"
        )

    def test_strips_non_gobby_slash_command(self) -> None:
        assert build_heuristic_title("/loop check the deploy") == "Check the deploy"
        assert build_heuristic_title("$skill do thing") == "Do thing"
        assert build_heuristic_title("/skill do thing") == "Do thing"

    def test_strips_single_slash_command_with_no_args(self) -> None:
        assert build_heuristic_title("/help") is None
        assert build_heuristic_title("/schedule") is None

    def test_plain_prompt_unaffected_by_slash_stripping(self) -> None:
        assert build_heuristic_title("hello world") == "Hello world"

    @pytest.mark.parametrize(
        "prompt",
        [
            "<user_query>",
            "<user_prompt>",
            "<prompt>",
            "<input>",
            "<USER_QUERY>",
        ],
    )
    def test_rejects_angle_bracket_placeholders(self, prompt: str) -> None:
        assert build_heuristic_title(prompt) is None


class TestNormalizeNativeTitle:
    """Tests for CLI-native title validation (Claude ai-title, Droid sessionTitle)."""

    def test_accepts_clean_title(self) -> None:
        assert normalize_native_title("Fix authentication bug") == "Fix authentication bug"

    def test_claude_native_title_replaces_slug_dashes(self) -> None:
        assert (
            normalize_native_title(
                "check-gobby-logs-for-tmux-warnings",
                source="claude",
            )
            == "check gobby logs for tmux warnings"
        )
        assert (
            normalize_native_title(
                "check-gobby-logs-for-tmux-warnings",
                source="droid",
            )
            == "check-gobby-logs-for-tmux-warnings"
        )

    @pytest.mark.parametrize(
        "title",
        [
            "<local-command>",
            '<local-command name="pwd">',
            "<local-command-stdout>",
            "<function_calls>",
            '<invoke name="test">',
            '<parameter name="x">',
        ],
    )
    def test_rejects_claude_native_tool_tag_titles(self, title: str) -> None:
        assert normalize_native_title(title, source="claude") is None

    def test_rejects_new_session_placeholder(self) -> None:
        assert normalize_native_title("New Session") is None
        assert normalize_native_title("new session") is None

    def test_rejects_multiline_content(self) -> None:
        assert normalize_native_title("Line one\nLine two") is None

    def test_rejects_excessively_long_content(self) -> None:
        assert normalize_native_title("A" * 201) is None

    def test_rejects_xml_block_content(self) -> None:
        assert normalize_native_title("I will help. <function_calls> stuff") is None
        assert normalize_native_title('<invoke name="test">') is None
        assert normalize_native_title('<parameter name="x">') is None

    def test_rejects_non_string(self) -> None:
        assert normalize_native_title(None) is None
        assert normalize_native_title(123) is None

    def test_rejects_empty(self) -> None:
        assert normalize_native_title("") is None
        assert normalize_native_title("   ") is None

    def test_truncates_long_but_valid_title(self) -> None:
        title = "A" * 100
        result = normalize_native_title(title)
        assert result is not None
        assert len(result) == 80

    def test_rejects_template_placeholder(self) -> None:
        assert normalize_native_title("<user_query>") is None


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


class TestShouldUpdateDigestTitle:
    """Tests for digest title ownership policy."""

    def test_missing_title_attrs_are_treated_as_untitled(self) -> None:
        class LegacySession:
            pass

        assert _should_update_digest_title(LegacySession()) is True

    @pytest.mark.parametrize("title_source", ["heuristic", "provisional"])
    def test_fallback_title_is_replaceable(self, title_source: str) -> None:
        session = MagicMock()
        session.title = "#42 codex"
        session.title_source = title_source

        assert _should_update_digest_title(session) is True

    @pytest.mark.parametrize(
        ("title", "title_source", "expected"),
        [
            ("Manual Title", "manual", False),
            ("Legacy Title", None, False),
            ("", None, True),
            ("", "native", True),
        ],
    )
    def test_manual_and_legacy_title_policy(
        self,
        title: str,
        title_source: str | None,
        expected: bool,
    ) -> None:
        session = MagicMock()
        session.title = title
        session.title_source = title_source

        assert _should_update_digest_title(session) is expected

    def test_non_empty_native_title_is_preserved(self) -> None:
        session = MagicMock()
        session.title = "Fix auth bug"
        session.title_source = "native"
        assert _should_update_digest_title(session) is False


class TestCanReplaceWithNativeTitle:
    """Tests for CLI-native title replacement policy."""

    def test_replaces_empty_title(self) -> None:
        session = MagicMock()
        session.title = ""
        session.title_source = ""
        assert _can_replace_with_native_title(session) is True

    def test_replaces_provisional_title(self) -> None:
        session = MagicMock()
        session.title = "#42 codex"
        session.title_source = "provisional"
        assert _can_replace_with_native_title(session) is True

    def test_replaces_heuristic_title(self) -> None:
        session = MagicMock()
        session.title = "Fix the auth"
        session.title_source = "heuristic"
        assert _can_replace_with_native_title(session) is True

    def test_replaces_existing_native_title(self) -> None:
        """Claude may emit multiple ai-title updates; latest wins."""
        session = MagicMock()
        session.title = "Old native title"
        session.title_source = "native"
        assert _can_replace_with_native_title(session) is True

    def test_denies_manual_title(self) -> None:
        session = MagicMock()
        session.title = "My custom title"
        session.title_source = "manual"
        assert _can_replace_with_native_title(session) is False

    def test_denies_llm_title(self) -> None:
        """LLM digest has more context; native should not override it."""
        session = MagicMock()
        session.title = "Comprehensive digest title"
        session.title_source = "llm"
        assert _can_replace_with_native_title(session) is False


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


class TestBootstrapSessionTitle:
    """Tests for bootstrap_session_title helper."""

    @pytest.mark.asyncio
    async def test_bootstraps_heuristic_title(self) -> None:
        session_manager = MagicMock()
        session = MagicMock()
        session.title = None
        session_manager.get.return_value = session
        session_manager.update_title.return_value = session

        title = await bootstrap_session_title(
            session_manager,
            "session-123",
            "Please fix the auth bug in login.py",
        )

        assert title == "Fix the auth bug in login.py"
        session_manager.update_title.assert_called_once_with(
            "session-123",
            "Fix the auth bug in login.py",
            title_source="heuristic",
        )

    @pytest.mark.asyncio
    async def test_replaces_provisional_title_from_prompt(self) -> None:
        session_manager = MagicMock()
        session = MagicMock()
        session.title = "#123 codex"
        session.title_source = "provisional"
        session_manager.get.return_value = session
        session_manager.update_title.return_value = session

        title = await bootstrap_session_title(
            session_manager,
            "session-123",
            "Please fix daemon tmux window titles",
        )

        assert title == "Fix daemon tmux window titles"
        session_manager.update_title.assert_called_once_with(
            "session-123",
            "Fix daemon tmux window titles",
            title_source="heuristic",
        )

    @pytest.mark.asyncio
    async def test_skips_when_title_already_exists(self) -> None:
        session_manager = MagicMock()
        session = MagicMock()
        session.title = "Existing Title"
        session_manager.get.return_value = session

        title = await bootstrap_session_title(session_manager, "session-123", "Fix the auth bug")

        assert title is None
        session_manager.update_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_derives_title_from_transcript_when_prompt_missing(self) -> None:
        """When the event carries no prompt, fall back to the transcript opener."""
        session_manager = MagicMock()
        session = MagicMock()
        session.title = None
        session.source = "claude"
        session.transcript_path = str(_CLAUDE_FIXTURE)
        session_manager.get.return_value = session
        session_manager.update_title.return_value = session

        # prompt_text is empty/None — heuristic must come from the transcript.
        title = await bootstrap_session_title(session_manager, "session-123", "")

        assert title == "Fix Claude session titles in VSCode"
        session_manager.update_title.assert_called_once_with(
            "session-123",
            "Fix Claude session titles in VSCode",
            title_source="heuristic",
        )

    @pytest.mark.asyncio
    async def test_returns_none_when_no_prompt_and_no_transcript(self) -> None:
        session_manager = MagicMock()
        session = MagicMock()
        session.title = None
        session.source = "claude"
        session.transcript_path = None
        session_manager.get.return_value = session

        title = await bootstrap_session_title(session_manager, "session-123", "")

        assert title is None
        session_manager.update_title.assert_not_called()


def _claude_user_record(content: object, idx: int = 0) -> dict[str, object]:
    """Build a realistic Claude transcript user record (full envelope)."""
    return {
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/projects/test",
        "sessionId": "sess",
        "version": "2.1.160",
        "type": "user",
        "message": {"role": "user", "content": content},
        "uuid": f"u-{idx}",
        "timestamp": "2026-06-01T10:00:00.000Z",
    }


def _claude_assistant_record(text: str, idx: int = 0) -> dict[str, object]:
    """Build a realistic Claude transcript assistant text record (full envelope)."""
    return {
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/projects/test",
        "sessionId": "sess",
        "version": "2.1.160",
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "uuid": f"a-{idx}",
        "timestamp": "2026-06-01T10:00:00.000Z",
    }


class TestHeuristicTitleFromTranscript:
    """Tests for heuristic_title_from_transcript (opening-prompt extraction)."""

    @pytest.mark.asyncio
    async def test_extracts_first_user_prompt_from_real_fixture(self) -> None:
        title = await heuristic_title_from_transcript(str(_CLAUDE_FIXTURE), "claude")
        # The opener wins — tool_result user turns and the later follow-up are
        # not used as the session title.
        assert title == "Fix Claude session titles in VSCode"

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_path(self) -> None:
        assert await heuristic_title_from_transcript("/nonexistent/path.jsonl", "claude") is None
        assert await heuristic_title_from_transcript(None, "claude") is None

    @pytest.mark.parametrize("source", [None, "", "unknown-cli"])
    async def test_skips_unsupported_source_with_log(
        self,
        source: str | None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING):
            title = await heuristic_title_from_transcript(str(_CLAUDE_FIXTURE), source)

        assert title is None
        assert "Skipping heuristic title" in caplog.text

    @pytest.mark.asyncio
    async def test_skips_lifecycle_and_tool_results(self, tmp_path: Path) -> None:
        import json

        transcript = tmp_path / "transcript.jsonl"
        records = [
            _claude_user_record("/clear", idx=1),
            _claude_user_record(
                [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}], idx=2
            ),
            _claude_user_record("Refactor the dispatcher rules", idx=3),
        ]
        transcript.write_text("\n".join(json.dumps(r) for r in records))

        title = await heuristic_title_from_transcript(str(transcript), "claude")
        assert title == "Refactor the dispatcher rules"

    @pytest.mark.asyncio
    async def test_skips_interrupt_control_markers(self, tmp_path: Path) -> None:
        import json

        transcript = tmp_path / "transcript.jsonl"
        records = [
            _claude_user_record("[Request interrupted by user]", idx=1),
            _claude_user_record("[Request interrupted by user for tool use]", idx=2),
            _claude_user_record("Investigate flaky digest titles", idx=3),
        ]
        transcript.write_text("\n".join(json.dumps(r) for r in records))

        title = await heuristic_title_from_transcript(str(transcript), "claude")
        assert title == "Investigate flaky digest titles"

    @pytest.mark.asyncio
    async def test_skips_synthetic_bootstrap_prompts(self, tmp_path: Path) -> None:
        import json

        transcript = tmp_path / "transcript.jsonl"
        records = [
            _claude_user_record("# AGENTS.md instructions for /repo\n\n<INSTRUCTIONS>", idx=1),
            _claude_user_record("<turn_aborted>The user interrupted.</turn_aborted>", idx=2),
            _claude_user_record("Fix the actual user request", idx=3),
        ]
        transcript.write_text("\n".join(json.dumps(r) for r in records))

        title = await heuristic_title_from_transcript(str(transcript), "claude")
        assert title == "Fix the actual user request"

    @pytest.mark.asyncio
    async def test_skips_command_only_router_prompts(self, tmp_path: Path) -> None:
        import json

        transcript = tmp_path / "transcript.jsonl"
        records = [
            _claude_user_record("$gobby coderabbit", idx=1),
            _claude_user_record("/gobby coderabbit", idx=2),
            _claude_user_record("$gobby coderabbit fix review comments", idx=3),
        ]
        transcript.write_text("\n".join(json.dumps(r) for r in records))

        title = await heuristic_title_from_transcript(str(transcript), "claude")
        assert title == "Fix review comments"

    @pytest.mark.asyncio
    async def test_opening_prompt_wins_on_long_transcript(self, tmp_path: Path) -> None:
        """The opening prompt wins even with a mid-session /clear, an
        assistant-heavy tail, tool_result user records, and >200 later turns —
        the exact shape the old last-window implementation returned None on.
        """
        import json

        records: list[dict[str, object]] = [
            _claude_user_record("Add pagination to the search endpoint", idx=0),
            _claude_user_record("/clear", idx=1),
        ]
        for i in range(250):
            records.append(_claude_assistant_record(f"Working on step {i}.", idx=i))
            records.append(
                _claude_user_record(
                    [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}],
                    idx=1000 + i,
                )
            )
        records.append(_claude_user_record("now also add sorting", idx=9999))

        transcript = tmp_path / "long.jsonl"
        transcript.write_text("\n".join(json.dumps(r) for r in records))

        title = await heuristic_title_from_transcript(str(transcript), "claude")
        assert title == "Add pagination to the search endpoint"


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
        service.call_feature = AsyncMock(
            side_effect=[
                _turn_record_json(
                    "User asked to fix a bug. Agent found the root cause in auth.py line 42.",
                    "Fix Auth Bug",
                ),
            ]
        )
        return service

    def test_fallback_prompts_instruct_titles_to_ignore_router_commands(self) -> None:
        turn_prompt = _build_turn_record_prompt("$gobby coderabbit fix comments", "Done")
        title_prompt = _build_title_synthesis_prompt("### Turn 1\nUser ran /help.")

        for prompt in (turn_prompt, title_prompt):
            assert "/gobby coderabbit" in prompt
            assert "$gobby coderabbit" in prompt
            assert "/help" in prompt
            assert "$skill" in prompt
            assert "Never" in prompt
            assert "`/` or `$`" in prompt

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
    async def test_successful_pipeline(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Test the full pipeline with prompt_text provided."""
        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the authentication bug in auth.py",
            llm_service=mock_llm_service,
            config=_digest_config(),
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
        assert mock_llm_service.call_feature.await_count == 1
        assert mock_llm_service.call_feature.await_args.kwargs["caller"] == "memory.turn_record"

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
        mock_llm_service.call_feature.side_effect = LLMProviderCancellation(
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
    async def test_cancellation_persists_public_heuristic_title_from_transcript(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """On provider cancellation, a transcript heuristic title still lands when
        the session has none — so a window name is set even without the LLM."""
        session = mock_session_manager.get.return_value
        session.title = None
        session.transcript_path = str(_CLAUDE_FIXTURE)
        session.source = "claude"

        mock_llm_service.call_feature.side_effect = LLMProviderCancellation(
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
        assert result["title"] == "Fix Claude session titles in VSCode"
        assert result["title_source"] == "heuristic"
        mock_session_manager.update_title.assert_called_once_with(
            "session-123",
            "Fix Claude session titles in VSCode",
            title_source="heuristic",
        )
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
    async def test_invalid_turn_record_json_retries_then_returns_error_without_persistence(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Invalid turn-record JSON fails after retries without persisting digest state."""
        mock_llm_service.call_feature = AsyncMock(return_value="not json")

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
        assert mock_llm_service.call_feature.await_count == 3
        mock_session_manager.persist_digest_state.assert_not_called()
        mock_session_manager.update_last_turn_markdown.assert_not_called()
        mock_session_manager.update_digest_markdown.assert_not_called()
        mock_session_manager.update_title.assert_not_called()
        mock_session_manager.update_last_digest_input_hash.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_turn_record_json_retries_and_recovers(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Transient invalid turn-record JSON retries and persists the valid response."""
        mock_llm_service.call_feature = AsyncMock(
            side_effect=[
                "not json",
                '{"turn_markdown":"","title_candidate":"Digest JSON Titles"}',
                _turn_record_json(
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
        assert mock_llm_service.call_feature.await_count == 3
        calls = mock_llm_service.call_feature.await_args_list
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
        mock_llm_service.call_feature = AsyncMock(
            side_effect=[
                "not json",
                _turn_record_json("User asked for a fix. Agent recovered.", "Recovered"),
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
            '{"title_candidate":"Digest JSON Titles"}',
            '{"turn_markdown":"","title_candidate":"Digest JSON Titles"}',
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
        mock_llm_service.call_feature = AsyncMock(return_value=response)

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
        mock_llm_service.call_feature = AsyncMock(return_value='{"turn_markdown":"Did the work"}')

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
        mock_llm_service.call_feature = AsyncMock(
            return_value=_turn_record_json(
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
        mock_llm_service.call_feature = AsyncMock(
            return_value=_turn_record_json(
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
        assert mock_llm_service.call_feature.await_count == 1
        assert mock_llm_service.call_feature.await_args.kwargs["caller"] == "memory.turn_record"

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
        mock_llm_service.call_feature = AsyncMock(
            return_value=_turn_record_json(
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
    async def test_refines_heuristic_title_once(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """A first-prompt heuristic title is refined after the first digest turn."""
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
    async def test_preserves_native_title_from_digest_title(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """A trusted native title is not replaced by digest title synthesis."""
        session = mock_session_manager.get.return_value
        session.title = "Native Session Title"
        session.title_source = "native"
        mock_llm_service.call_feature = AsyncMock(
            return_value=_turn_record_json(
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
        assert "title" not in result
        mock_session_manager.persist_digest_state.assert_called_once()
        assert mock_session_manager.persist_digest_state.call_args.kwargs["title"] is None
        assert mock_session_manager.persist_digest_state.call_args.kwargs["title_source"] is None
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
        mock_llm_service.call_feature = AsyncMock(
            return_value=_turn_record_json(
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
        assert mock_llm_service.call_feature.await_count == 1

    @pytest.mark.asyncio
    async def test_preserves_non_empty_legacy_unknown_title(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """A non-empty title with title_source=None is treated as legacy owned."""
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
        assert "title" not in result
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
        call_args = mock_llm_service.call_feature.call_args_list[0]
        prompt = call_args.args[1]
        assert "Implement the feature" in prompt or "feature" in prompt.lower()
        assert call_args.kwargs["caller"] == "memory.turn_record"

    @pytest.mark.asyncio
    async def test_synthesize_title_respects_digest_timeout(self):
        """Digest title synthesis uses digest.timeout to bound LLM latency."""
        session_manager = MagicMock()
        session = MagicMock()
        session.title = None
        llm_service = MagicMock()

        async def _slow_call(*args, **kwargs):
            await wait_forever()
            return "Too Slow"

        llm_service.call_feature = AsyncMock(side_effect=_slow_call)
        digest_config = MagicMock(timeout=0.01)

        with pytest.raises(TimeoutError):
            await _synthesize_title(
                updated_digest="### Turn 1\nSomething happened",
                session_id="session-123",
                session_manager=session_manager,
                session=session,
                llm_service=llm_service,
                digest_config=digest_config,
            )

        assert llm_service.call_feature.await_args.kwargs["caller"] == "memory.title_synthesis"

    async def test_synthesize_title_bounds_digest_excerpt(self):
        """Title recovery sends only the most recent bounded digest excerpt."""
        session_manager = MagicMock()
        session_manager.update_title.return_value = MagicMock()
        session = MagicMock(title=None, title_source=None)
        llm_service = MagicMock()
        llm_service.call_feature = AsyncMock(return_value="Bounded Title")
        digest_config = MagicMock(timeout=1)
        digest = "head-marker" + ("x" * 12_000) + "tail-marker"

        with patch(
            "gobby.memory.digest._render_prompt_template",
            side_effect=RuntimeError("use inline fallback"),
        ):
            await _synthesize_title(
                updated_digest=digest,
                session_id="session-123",
                session_manager=session_manager,
                session=session,
                llm_service=llm_service,
                digest_config=digest_config,
            )

        prompt = llm_service.call_feature.await_args.args[1]
        assert "head-marker" not in prompt
        assert "tail-marker" in prompt


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
        session.title = None
        session.title_source = None
        session.seq_num = 42
        session.terminal_context = None
        session.last_digest_input_hash = None  # No prior digest
        session.last_title_synthesis_digest_hash = None
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
        service.call_feature = AsyncMock(
            side_effect=[
                _turn_record_json("User asked to fix a bug. Agent found the root cause."),
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
        mock_llm_service.call_feature.assert_not_called()

    @pytest.mark.asyncio
    async def test_synthesizes_missing_title_from_existing_digest_when_duplicate(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Duplicate digest input still backfills a missing title from existing digest."""
        import hashlib

        prompt = "Fix the bug"
        expected_hash = hashlib.sha256(f"0||{prompt}||".encode()).hexdigest()[:16]
        session = mock_session_manager.get.return_value
        session.digest_markdown = "### Turn 1\nExisting digest"
        session.last_digest_input_hash = expected_hash
        session.last_title_synthesis_digest_hash = "older-digest"

        mock_llm_service.call_feature = AsyncMock(return_value="Recovered Title")

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text=prompt,
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result == {
            "title": "Recovered Title",
            "title_only": True,
            "digest_length": len(session.digest_markdown),
        }
        mock_session_manager.update_title.assert_called_once_with(
            "session-123",
            "Recovered Title",
            title_source="llm",
        )
        expected_digest_hash = hashlib.sha256(session.digest_markdown.encode()).hexdigest()[:16]
        mock_session_manager.update_last_title_synthesis_digest_hash.assert_called_once_with(
            "session-123", expected_digest_hash
        )
        mock_session_manager.persist_digest_state.assert_not_called()
        mock_session_manager.update_last_turn_markdown.assert_not_called()
        mock_session_manager.update_digest_markdown.assert_not_called()
        mock_session_manager.update_last_digest_input_hash.assert_not_called()
        assert mock_llm_service.call_feature.await_count == 1
        assert mock_llm_service.call_feature.await_args.kwargs["caller"] == "memory.title_synthesis"

    @pytest.mark.asyncio
    async def test_unchanged_digest_does_not_repeat_title_synthesis(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """An unchanged digest is attempted at most once for title recovery."""
        import hashlib

        prompt = "Fix the bug"
        expected_hash = hashlib.sha256(f"0||{prompt}||".encode()).hexdigest()[:16]
        session = mock_session_manager.get.return_value
        session.digest_markdown = "### Turn 1\nExisting digest"
        session.last_digest_input_hash = expected_hash
        session.last_title_synthesis_digest_hash = hashlib.sha256(
            session.digest_markdown.encode()
        ).hexdigest()[:16]

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text=prompt,
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is None
        mock_llm_service.call_feature.assert_not_called()
        mock_session_manager.update_last_title_synthesis_digest_hash.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_title_synthesis_when_title_present_and_duplicate(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Duplicate digest input does not hit the title LLM when title already exists."""
        import hashlib

        prompt = "Fix the bug"
        expected_hash = hashlib.sha256(f"0||{prompt}||".encode()).hexdigest()[:16]
        session = mock_session_manager.get.return_value
        session.digest_markdown = "### Turn 1\nExisting digest"
        session.last_digest_input_hash = expected_hash
        session.title = "Existing Title"
        session.title_source = "manual"

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text=prompt,
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is None
        mock_llm_service.call_feature.assert_not_called()
        mock_session_manager.update_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_synthesizes_provisional_title_from_existing_digest_when_duplicate(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Duplicate digest input still replaces a provisional title."""
        import hashlib

        prompt = "Fix the bug"
        expected_hash = hashlib.sha256(f"0||{prompt}||".encode()).hexdigest()[:16]
        session = mock_session_manager.get.return_value
        session.digest_markdown = "### Turn 1\nExisting digest"
        session.last_digest_input_hash = expected_hash
        session.title = "#42 codex"
        session.title_source = "provisional"

        mock_llm_service.call_feature = AsyncMock(return_value="Recovered Title")

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text=prompt,
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result == {
            "title": "Recovered Title",
            "title_only": True,
            "digest_length": len(session.digest_markdown),
        }
        mock_session_manager.update_title.assert_called_once_with(
            "session-123",
            "Recovered Title",
            title_source="llm",
        )
        mock_session_manager.persist_digest_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_title_synthesis_when_no_digest_and_duplicate(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Duplicate digest input without an existing digest still skips fully."""
        import hashlib

        prompt = "Fix the bug"
        expected_hash = hashlib.sha256(f"0||{prompt}||".encode()).hexdigest()[:16]
        session = mock_session_manager.get.return_value
        session.last_digest_input_hash = expected_hash
        session.digest_markdown = None
        session.title = None
        session.title_source = None

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text=prompt,
            llm_service=mock_llm_service,
            config=_digest_config(),
        )

        assert result is None
        mock_llm_service.call_feature.assert_not_called()
        mock_session_manager.update_title.assert_not_called()

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

    def _write_claude_transcript(self, path, exchanges):
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
        result, next_index = await _read_undigested_turns("/nonexistent/path.jsonl", "claude", 0)
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
            result, next_index = await _read_undigested_turns(str(transcript), source, 0)

        assert result == []
        assert next_index == 0
        assert "Skipping transcript" in caplog.text

    @pytest.mark.asyncio
    async def test_single_pair_backward_compat(self, tmp_path) -> None:
        """With 1 pair and 0 digested, returns that single pair."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(transcript, [("Hello", "Hi there")])

        result, next_index = await _read_undigested_turns(str(transcript), "claude", 0)
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

        result, next_index = await _read_undigested_turns(str(transcript), "claude", 1)
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

        result, next_index = await _read_undigested_turns(
            str(transcript), "claude", 50, num_pairs=50
        )

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

        result, _ = await _read_undigested_turns(str(transcript), "claude", 0)
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
                            "content": "<command-name>/clear</command-name>",
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

        result, _ = await _read_undigested_turns(str(transcript), "claude", 0)
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
                        "content": "<command-name>/clear</command-name>",
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

        result, next_index = await _read_undigested_turns(str(transcript), "claude", 1)
        repeated, repeated_index = await _read_undigested_turns(
            str(transcript), "claude", next_index
        )
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
        later, later_index = await _read_undigested_turns(str(transcript), "claude", repeated_index)

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
                        "content": "<command-name>/clear</command-name>",
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

        result, next_index = await _read_undigested_turns(str(transcript), "claude", 1)
        repeated, repeated_index = await _read_undigested_turns(
            str(transcript), "claude", next_index
        )

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

        result, _ = await _read_undigested_turns(str(transcript), "claude", 0)
        assert len(result) == 2
        assert result[0] == ("Interrupted question", "")
        assert result[1] == ("Follow-up question", "Final answer")

    @pytest.mark.asyncio
    async def test_hook_blocking_error_tool_result_counts_once(self, tmp_path) -> None:
        """Claude hook block duplicate records do not create extra digest exchanges."""
        transcript = tmp_path / "transcript.jsonl"
        import json

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
                {
                    "type": "system",
                    "subtype": "hook_blocking_error",
                    "toolUseID": "toolu_blocked",
                    "content": "Gobby blocked [require-uv]: Use uv instead.",
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_blocked",
                                "content": "Gobby blocked [require-uv]: Use uv instead.",
                                "is_error": True,
                            }
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "I will use uv instead."}],
                    },
                },
            ):
                f.write(json.dumps(turn) + "\n")

        result, _ = await _read_undigested_turns(str(transcript), "claude", 0)

        assert result == [("Run the command", "I will use uv instead.")]

    @pytest.mark.asyncio
    async def test_cursor_past_new_segment_resets_to_segment_start(self, tmp_path) -> None:
        """A /clear-style segment reset consumes the whole replacement segment."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [("Q1", "A1"), ("Q2", "A2")],
        )

        result, next_index = await _read_undigested_turns(str(transcript), "claude", 5)
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

        result, _ = await _read_undigested_turns(str(transcript), "claude", 0)

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
        service.call_feature = AsyncMock(
            side_effect=[
                _turn_record_json(
                    "User asked two questions. Agent answered both.",
                    "Multi-Exchange Session",
                ),
            ]
        )
        return service

    def _write_claude_transcript(self, path, exchanges):
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
        turn_prompt_call = mock_llm_service.call_feature.call_args_list[0]
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
        remaining, next_index = await _read_undigested_turns(
            str(transcript), "claude", persisted_index
        )
        assert remaining == []
        assert next_index == 3

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
        prompt = mock_llm_service.call_feature.await_args.args[1]
        assert "Question 51" in prompt
        assert "Answer 51" in prompt
        assert sm.persist_digest_state.call_args.kwargs["last_digested_pair_index"] == 51

    @pytest.mark.asyncio
    async def test_idempotency_combined_hash(
        self,
        mock_memory_manager,
        mock_llm_service,
        tmp_path,
    ):
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
        mock_llm_service.call_feature.assert_not_called()
