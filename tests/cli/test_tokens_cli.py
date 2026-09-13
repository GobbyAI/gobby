from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import click
import pytest
from click.testing import CliRunner

tokens_module = importlib.import_module("gobby.cli.tokens")

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.mark.parametrize(
    "arguments",
    [["audit", "--all", "--fix"], ["audit", "--session", "#1"], ["stats"]],
)
def test_unknown_project_fails_before_token_ledger_access(
    runner: CliRunner, arguments: list[str]
) -> None:
    project_manager = Mock()
    project_manager.get.return_value = None
    project_manager.get_by_name.return_value = None
    project_facade = SimpleNamespace(LocalProjectManager=Mock(return_value=project_manager))
    with (
        patch("gobby.cli.utils_resolution.facade", return_value=project_facade),
        patch("gobby.cli.runtime.require_cli_database"),
        patch.object(tokens_module, "require_cli_database") as ledger_database,
    ):
        result = runner.invoke(tokens_module.tokens, [*arguments, "--project", "unknown-project"])

    assert result.exit_code == 1
    assert "Project not found: unknown-project" in result.output
    ledger_database.assert_not_called()


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

    mock_parser = Mock()
    mock_parser.parse_lines.side_effect = ValueError("boom")
    monkeypatch.setattr(tokens_module, "get_parser", lambda *_args, **_kwargs: mock_parser)

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        pytest.raises(click.ClickException, match="Failed to parse transcript"),
    ):
        tokens_module._load_session_messages("sess-1", session)


def test_load_session_messages_rejects_unknown_source(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    session = SimpleNamespace(transcript_path=str(transcript), source="unknown-cli")

    with pytest.raises(click.ClickException, match="Unsupported transcript source"):
        tokens_module._load_session_messages("sess-1", session)


def test_audit_all_filters_by_project(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_db = _FakeDatabase([{"id": "sess-1"}])
    fake_manager = SimpleNamespace(get=_make_session)

    def resolve_project_ref(_ref: str | None, exit_on_not_found: bool = False) -> str:
        return "proj-1"

    def require_cli_database(ctx: object | None = None) -> _FakeDatabase:
        return fake_db

    def session_manager(_db: _FakeDatabase) -> SimpleNamespace:
        return fake_manager

    def token_event_store(_db: _FakeDatabase) -> _FakeStore:
        return _FakeStore()

    def load_session_messages(*_args: object, **_kwargs: object) -> list[Any]:
        return []

    monkeypatch.setattr(tokens_module, "resolve_project_ref", resolve_project_ref)
    monkeypatch.setattr(tokens_module, "require_cli_database", require_cli_database)
    monkeypatch.setattr(tokens_module, "SessionManager", session_manager)
    monkeypatch.setattr(tokens_module, "TokenEventStore", token_event_store)
    monkeypatch.setattr(tokens_module, "_load_session_messages", load_session_messages)

    result = runner.invoke(tokens_module.tokens, ["audit", "--all", "--project", "proj-1"])

    assert result.exit_code == 0
    query, params = fake_db.fetchall_calls[0]
    assert "AND project_id = %s" in query
    assert params == ("proj-1",)


def test_audit_all_continues_after_transcript_failure(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_db = _FakeDatabase([{"id": "sess-1"}, {"id": "sess-2"}])
    fake_manager = SimpleNamespace(get=_make_session)

    def _load_messages(session_id: str, session: Any) -> list[Any]:
        if session_id == "sess-1":
            raise click.ClickException("bad transcript")
        return []

    def require_cli_database(ctx: object | None = None) -> _FakeDatabase:
        return fake_db

    def session_manager(_db: _FakeDatabase) -> SimpleNamespace:
        return fake_manager

    def token_event_store(_db: _FakeDatabase) -> _FakeStore:
        return _FakeStore()

    monkeypatch.setattr(tokens_module, "require_cli_database", require_cli_database)
    monkeypatch.setattr(tokens_module, "SessionManager", session_manager)
    monkeypatch.setattr(tokens_module, "TokenEventStore", token_event_store)
    monkeypatch.setattr(tokens_module, "_load_session_messages", _load_messages)

    result = runner.invoke(tokens_module.tokens, ["audit", "--all"])

    assert result.exit_code == 0
    assert "sess-1: bad transcript" in result.output
    assert "sess-2: ok" in result.output
    assert "audited=1 drifted=0" in result.output
