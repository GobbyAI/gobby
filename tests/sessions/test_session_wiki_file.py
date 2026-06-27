"""Unit tests for the flat session-wiki file writer."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from gobby.sessions import session_wiki_file as swf

pytestmark = pytest.mark.unit


def _session(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "external_id": "019de70c-646f-7bd2-a31d-2e626de30891",
        "id": "11111111-2222-3333-4444-555555555555",
        "created_at": "2026-05-02T12:00:00Z",
        "model": "claude-opus-4-8",
        "project_id": "d45545c5-ded5-4335-b115-0245752edacf",
        "source": "codex",
        "agent_depth": 0,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_redact_session_markdown_scrubs_secrets() -> None:
    text = (
        "key sk-ABCDEFGHIJKLMNOPQRSTUV and token ghp_ABCDEFGHIJKLMNOPQR "
        "plus github_pat_1234567890_abcdefghijklmnopqrstuv"
    )
    redacted = swf.redact_session_markdown(text)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUV" not in redacted
    assert "sk-<redacted>" in redacted
    assert "ghp_ABCDEFGHIJKLMNOPQR" not in redacted
    assert "github_pat_1234567890_abcdefghijklmnopqrstuv" not in redacted


def test_redact_session_markdown_rewrites_home_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(swf.Path, "home", lambda: Path("/Users/secrethome"))
    redacted = swf.redact_session_markdown("see /Users/secrethome/.gobby/x")
    assert "/Users/secrethome" not in redacted
    assert "~/.gobby/x" in redacted


def test_build_frontmatter_emits_flat_keys() -> None:
    frontmatter = swf._build_frontmatter(_session(), [])
    lines = frontmatter.splitlines()
    assert lines[0] == "---"
    assert lines[-1] == "---"
    body = lines[1:-1]
    keys = {line.split(":", 1)[0] for line in body if ":" in line}
    assert {"title", "type", "tags", "date", "model", "project", "session_id", "source"} <= keys
    assert "tags: []" in body
    assert "type: source" in body
    assert "source: codex" in body
    assert "project: d45545c5-ded5-4335-b115-0245752edacf" in body


def test_resolve_session_wiki_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gobby.cli.utils_config.get_gobby_home", lambda: tmp_path)
    path = swf.resolve_session_wiki_path(_session())
    assert path == tmp_path / "session_wiki" / "019de70c-646f-7bd2-a31d-2e626de30891.md"


def test_write_session_wiki_page_writes_redacted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gobby.cli.utils_config.get_gobby_home", lambda: tmp_path)
    summary = "## Current State\nShipped the fix. Leaked sk-ABCDEFGHIJKLMNOPQRSTUV here."

    result = swf.write_session_wiki_page(_session(), summary)

    assert result["written"] is True
    path = tmp_path / "session_wiki" / "019de70c-646f-7bd2-a31d-2e626de30891.md"
    assert path == Path(result["path"])
    content = path.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert "## Current State" in content
    assert "sk-ABCDEFGHIJKLMNOPQRSTUV" not in content  # redacted before write
    assert "sk-<redacted>" in content


def test_write_session_wiki_page_skips_invalid_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gobby.cli.utils_config.get_gobby_home", lambda: tmp_path)
    result = swf.write_session_wiki_page(_session(), "   ")
    assert result == {"written": False, "skipped": "invalid_summary"}
    assert not (tmp_path / "session_wiki").exists()


def test_write_session_wiki_page_reports_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gobby.cli.utils_config.get_gobby_home", lambda: tmp_path)
    monkeypatch.setattr(swf, "_write_file", lambda _path, _content: False)

    result = swf.write_session_wiki_page(_session(), "## Current State\nwork")

    assert result["written"] is False
    assert result["skipped"] == "write_failed"


def test_write_session_wiki_page_skips_subagent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gobby.cli.utils_config.get_gobby_home", lambda: tmp_path)
    result = swf.write_session_wiki_page(_session(agent_depth=1), "## Current State\nwork")
    assert result == {"written": False, "skipped": "subagent"}


@pytest.mark.parametrize("source", ["pipeline", "cron"])
def test_write_session_wiki_page_skips_ephemeral_source(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gobby.cli.utils_config.get_gobby_home", lambda: tmp_path)
    result = swf.write_session_wiki_page(_session(source=source), "## Current State\nwork")
    assert result == {"written": False, "skipped": f"ephemeral_source:{source}"}


def test_session_wiki_path_exists_tracks_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gobby.cli.utils_config.get_gobby_home", lambda: tmp_path)
    session = _session()
    assert swf.session_wiki_path_exists(session) is False  # missing → restore needed
    swf.write_session_wiki_page(session, "## Current State\nwork")
    assert swf.session_wiki_path_exists(session) is True


def test_session_wiki_path_is_fresh_uses_summary_generated_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gobby.cli.utils_config.get_gobby_home", lambda: tmp_path)
    session = _session(summary_generated_at="2026-05-02T12:00:00+00:00")
    path = swf.resolve_session_wiki_path(session)
    path.parent.mkdir(parents=True)
    path.write_text("old", encoding="utf-8")
    old_mtime = 1_000_000_000
    os.utime(path, (old_mtime, old_mtime))

    assert swf.session_wiki_path_is_fresh(session) is False

    future_mtime = 2_000_000_000
    os.utime(path, (future_mtime, future_mtime))

    assert swf.session_wiki_path_is_fresh(session) is True
