"""Tests for Session model to_brief() slim representation."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.storage.session_models import Session

pytestmark = pytest.mark.unit


def _make_session(**overrides: Any) -> Session:
    """Create a Session with sensible defaults."""
    defaults = {
        "id": "sess-abc123",
        "external_id": "ext-123",
        "machine_id": "machine-1",
        "source": "claude",
        "project_id": "proj-xyz",
        "title": "Test session",
        "status": "active",
        "transcript_path": "/tmp/transcripts/sess.jsonl",
        "summary_path": "/tmp/summaries/sess.md",
        "summary_markdown": "# Summary\nLong markdown content...",
        "git_branch": "main",
        "parent_session_id": None,
        "created_at": "2026-01-22T00:00:00+00:00",
        "updated_at": "2026-01-22T12:00:00+00:00",
        "seq_num": 42,
        "model": "claude-sonnet-4-6",
        "had_edits": True,
        "digest_markdown": "# Digest\nRolling digest...",
        "last_turn_markdown": "# Turn\nLast turn details...",
        "usage_input_tokens": 50000,
        "usage_output_tokens": 10000,
        "context_window": 200000,
        "terminal_context": {"tty": "/dev/ttys001"},
    }
    defaults.update(overrides)
    return Session(**defaults)


class TestSessionToBrief:
    """Tests for Session.to_brief() slim representation."""

    def test_to_brief_has_fewer_fields_than_to_dict(self) -> None:
        """to_brief returns significantly fewer fields than to_dict."""
        session = _make_session()

        brief = session.to_brief()
        full = session.to_dict()
        assert len(brief) < len(full)
        # Session has 34 fields in to_dict, 13 in to_brief — big difference
        assert len(full) - len(brief) >= 15

    def test_to_brief_essential_fields_present(self) -> None:
        """to_brief includes essential identification and status fields."""
        session = _make_session()

        brief = session.to_brief()
        assert brief["ref"] == "#42"
        assert brief["external_id"] == "ext-123"
        assert brief["source"] == "claude"
        assert brief["project_id"] == "proj-xyz"
        assert brief["title"] == "Test session"
        assert brief["status"] == "active"
        assert brief["git_branch"] == "main"
        assert brief["model"] == "claude-sonnet-4-6"
        assert brief["had_edits"] is True
        assert brief["created_at"] == "2026-01-22T00:00:00+00:00"
        assert brief["updated_at"] == "2026-01-22T12:00:00+00:00"
        assert brief["seq_num"] == 42
        assert brief["id"] == "sess-abc123"

    def test_to_brief_excludes_markdown_blobs(self) -> None:
        """to_brief omits all markdown content fields."""
        session = _make_session()

        brief = session.to_brief()
        assert "summary_markdown" not in brief
        assert "digest_markdown" not in brief
        assert "last_turn_markdown" not in brief
        assert "original_prompt" not in brief

    def test_to_brief_excludes_paths(self) -> None:
        """to_brief omits file paths."""
        session = _make_session()

        brief = session.to_brief()
        assert "transcript_path" not in brief
        assert "summary_path" not in brief

    def test_to_brief_excludes_usage_metrics(self) -> None:
        """to_brief omits usage tracking fields."""
        session = _make_session()

        brief = session.to_brief()
        assert "usage_input_tokens" not in brief
        assert "usage_output_tokens" not in brief
        assert "usage_cache_creation_tokens" not in brief
        assert "usage_cache_read_tokens" not in brief
        assert "context_window" not in brief

    def test_to_brief_excludes_internal_metadata(self) -> None:
        """to_brief omits internal agent/workflow metadata."""
        session = _make_session()

        brief = session.to_brief()
        assert "machine_id" not in brief
        assert "agent_depth" not in brief
        assert "spawned_by_agent_id" not in brief
        assert "agent_run_id" not in brief
        assert "workflow_name" not in brief
        assert "parent_session_id" not in brief
        assert "context_injected" not in brief
        assert "terminal_context" not in brief
        assert "chat_mode" not in brief
        assert "last_digest_input_hash" not in brief

    def test_to_brief_ref_fallback_without_seq_num(self) -> None:
        """to_brief ref falls back to truncated UUID when seq_num is None."""
        session = _make_session(seq_num=None)

        brief = session.to_brief()
        assert brief["ref"] == "sess-abc"  # first 8 chars of id
