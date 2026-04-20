from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import pytest
from click.testing import CliRunner

tokens_module = importlib.import_module("gobby.cli.tokens")

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class _FakeDatabase:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.fetchall_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    def fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        self.fetchall_calls.append((query, params))
        return self.rows

    def close(self) -> None:
        self.closed = True


class _FakeStore:
    def get_session_totals(self, session_id: str) -> dict[str, int]:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }


def _make_session(session_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=session_id,
        transcript_path=f"/tmp/{session_id}.jsonl",
        project_id="proj-1",
        source="claude",
        context_window=None,
        usage_input_tokens=0,
        usage_output_tokens=0,
        usage_cache_creation_tokens=0,
        usage_cache_read_tokens=0,
    )


def test_load_session_messages_wraps_parse_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "bad-session.jsonl"
    transcript.write_text("{}", encoding="utf-8")
    session = SimpleNamespace(transcript_path=str(transcript), source="claude")

    def _raise_parse_error(self, lines, start_index=0):  # type: ignore[no-untyped-def]
        raise ValueError("boom")

    monkeypatch.setattr(tokens_module.ClaudeTranscriptParser, "parse_lines", _raise_parse_error)

    with pytest.raises(click.ClickException, match="Failed to parse transcript"):
        tokens_module._load_session_messages("sess-1", session)


def test_audit_all_filters_by_project(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db = _FakeDatabase([{"id": "sess-1"}])
    fake_manager = SimpleNamespace(get=lambda session_id: _make_session(session_id))

    monkeypatch.setattr(
        tokens_module,
        "resolve_project_ref",
        lambda _ref, exit_on_not_found=False: "proj-1",
    )
    monkeypatch.setattr(tokens_module, "LocalDatabase", lambda: fake_db)
    monkeypatch.setattr(tokens_module, "run_migrations", lambda db: None)
    monkeypatch.setattr(tokens_module, "LocalSessionManager", lambda db: fake_manager)
    monkeypatch.setattr(tokens_module, "TokenEventStore", lambda db: _FakeStore())
    monkeypatch.setattr(tokens_module, "_load_session_messages", lambda *_args, **_kwargs: [])

    result = runner.invoke(tokens_module.tokens, ["audit", "--all", "--project", "proj-1"])

    assert result.exit_code == 0
    query, params = fake_db.fetchall_calls[0]
    assert "AND project_id = ?" in query
    assert params == ("proj-1",)


def test_audit_all_continues_after_transcript_failure(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_db = _FakeDatabase([{"id": "sess-1"}, {"id": "sess-2"}])
    fake_manager = SimpleNamespace(get=lambda session_id: _make_session(session_id))

    def _load_messages(session_id: str, session: Any) -> list[Any]:
        if session_id == "sess-1":
            raise click.ClickException("bad transcript")
        return []

    monkeypatch.setattr(tokens_module, "LocalDatabase", lambda: fake_db)
    monkeypatch.setattr(tokens_module, "run_migrations", lambda db: None)
    monkeypatch.setattr(tokens_module, "LocalSessionManager", lambda db: fake_manager)
    monkeypatch.setattr(tokens_module, "TokenEventStore", lambda db: _FakeStore())
    monkeypatch.setattr(tokens_module, "_load_session_messages", _load_messages)

    result = runner.invoke(tokens_module.tokens, ["audit", "--all"])

    assert result.exit_code == 0
    assert "sess-1: bad transcript" in result.output
    assert "sess-2: ok" in result.output
    assert "audited=1 drifted=0" in result.output
