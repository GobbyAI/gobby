"""search_session_messages bounds its work per call and resumes by cursor (#23117)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import _messages
from gobby.mcp_proxy.tools.sessions._messages import register_message_tools
from gobby.sessions import transcript_reader as reader_module
from gobby.sessions.transcript_index import clear_index_cache
from gobby.sessions.transcript_reader import TranscriptReader, clear_archive_cache
from gobby.sessions.transcript_window import render_window

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


class _WorkCounter:
    """Counts snapshot resolutions and rendered groups across one search call."""

    def __init__(self) -> None:
        self.resolutions = 0
        self.groups_rendered = 0


@pytest.fixture(autouse=True)
def _local_sessions(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def require_local_ownership(session: Any) -> str:
        if getattr(session, "foreign", False):
            raise PermissionError(f"session {session.id} is owned by another machine")
        return LOCAL_MACHINE_ID

    monkeypatch.setattr(reader_module, "require_local_session_ownership", require_local_ownership)
    clear_archive_cache()
    clear_index_cache()
    yield
    clear_archive_cache()
    clear_index_cache()


@pytest.fixture
def work(monkeypatch: pytest.MonkeyPatch) -> _WorkCounter:
    counter = _WorkCounter()
    resolve = TranscriptReader._resolve_windowable

    async def counting_resolve(self: TranscriptReader, session: Any, session_id: str) -> Any:
        counter.resolutions += 1
        return await resolve(self, session, session_id)

    def counting_render(*args: Any, **kwargs: Any) -> Any:
        result = render_window(*args, **kwargs)
        counter.groups_rendered += result.returned_count
        return result

    monkeypatch.setattr(TranscriptReader, "_resolve_windowable", counting_resolve)
    monkeypatch.setattr("gobby.sessions.transcript_reader.render_window", counting_render)
    return counter


def _transcript(path: Path, texts: list[str]) -> Path:
    with path.open("w", encoding="utf-8") as handle:
        for text in texts:
            line = {"type": "user", "message": {"role": "user", "content": text}}
            handle.write(json.dumps(line) + "\n")
    return path


def _session(
    session_id: str, path: Path, *, created_day: int = 1, foreign: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(
        id=session_id,
        external_id=f"no-archive-{session_id}",
        source="claude",
        transcript_path=str(path),
        machine_id=LOCAL_MACHINE_ID,
        created_at=datetime(2026, 9, created_day, tzinfo=UTC),
        foreign=foreign,
    )


def _manager(sessions: list[SimpleNamespace]) -> MagicMock:
    by_id = {session.id: session for session in sessions}
    manager = MagicMock()
    manager.resolve_session_reference.side_effect = lambda ref, _project_id=None: ref
    manager.get.side_effect = by_id.get
    manager.list.return_value = sessions
    return manager


def _registry_for(manager: MagicMock) -> InternalToolRegistry:
    registry = InternalToolRegistry(name="gobby-sessions", description="test")
    register_message_tools(registry, manager, TranscriptReader(manager))
    return registry


def _registry(sessions: list[SimpleNamespace]) -> InternalToolRegistry:
    return _registry_for(_manager(sessions))


async def _search(registry: InternalToolRegistry, **arguments: Any) -> dict[str, Any]:
    result = await registry.call("search_session_messages", arguments)
    assert isinstance(result, dict)
    return result


def _contents(result: dict[str, Any]) -> list[str]:
    return [hit["message"]["content"] for hit in result["results"]]


@pytest.mark.asyncio
async def test_a_miss_renders_one_budget_from_one_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, work: _WorkCounter
) -> None:
    monkeypatch.setattr(_messages, "SEARCH_GROUP_BUDGET", 40, raising=False)
    path = _transcript(tmp_path / "long.jsonl", [f"line {i}" for i in range(200)])
    registry = _registry([_session("sess-long", path)])

    result = await _search(registry, query="absent needle", session_id="sess-long")

    assert result["success"] is True
    assert result["results"] == []
    assert result["truncated"] is True
    assert result.get("next_cursor")
    assert work.groups_rendered <= 40
    assert work.resolutions == 1


@pytest.mark.asyncio
async def test_a_large_history_miss_stays_within_the_default_budget(
    tmp_path: Path, work: _WorkCounter
) -> None:
    groups = 10 * _messages.SEARCH_GROUP_BUDGET
    path = _transcript(tmp_path / "large.jsonl", [f"history line {i}" for i in range(groups)])
    registry = _registry([_session("sess-large", path)])

    result = await _search(registry, query="absent needle", session_id="sess-large")

    assert result["truncated"] is True
    assert work.groups_rendered == _messages.SEARCH_GROUP_BUDGET
    assert work.resolutions == 1


@pytest.mark.asyncio
async def test_cursor_pages_return_every_match_in_order_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, work: _WorkCounter
) -> None:
    monkeypatch.setattr(_messages, "SEARCH_GROUP_BUDGET", 40, raising=False)
    texts = [f"Needle {i}" if i % 37 == 0 else f"line {i}" for i in range(200)]
    path = _transcript(tmp_path / "long.jsonl", texts)
    registry = _registry([_session("sess-long", path)])

    found: list[str] = []
    cursor: str | None = None
    calls = 0
    while True:
        arguments: dict[str, Any] = {"query": "needle", "session_id": "sess-long", "limit": 2}
        if cursor is not None:
            arguments["cursor"] = cursor
        result = await _search(registry, **arguments)
        calls += 1
        assert result["success"] is True
        assert result["returned_count"] <= 2
        found.extend(_contents(result))
        cursor = result.get("next_cursor")
        if cursor is None:
            assert result["truncated"] is False
            break

    assert found == [text for text in texts if text.startswith("Needle")]
    assert calls >= 200 // 40
    assert work.resolutions == calls


@pytest.mark.asyncio
async def test_multi_session_budget_spans_sessions_and_resumes_in_list_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, work: _WorkCounter
) -> None:
    monkeypatch.setattr(_messages, "SEARCH_GROUP_BUDGET", 50, raising=False)
    first = _transcript(tmp_path / "a.jsonl", [f"a {i}" for i in range(30)] + ["a needle"])
    second = _transcript(tmp_path / "b.jsonl", [f"b {i}" for i in range(60)] + ["b needle"])
    registry = _registry(
        [_session("sess-a", first, created_day=2), _session("sess-b", second, created_day=1)]
    )

    page_one = await _search(registry, query="NEEDLE")
    assert _contents(page_one) == ["a needle"]
    assert page_one["truncated"] is True
    assert work.groups_rendered <= 50

    page_two = await _search(registry, query="NEEDLE", cursor=page_one.get("next_cursor"))
    assert _contents(page_two) == ["b needle"]
    assert [hit["session_id"] for hit in page_two["results"]] == ["sess-b"]
    assert page_two.get("next_cursor") is None
    assert page_two["truncated"] is False


@pytest.mark.asyncio
async def test_cursor_pages_survive_a_recency_reorder_between_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_messages, "SEARCH_GROUP_BUDGET", 50)
    sessions = [
        _session(
            f"sess-{name}",
            _transcript(
                tmp_path / f"{name}.jsonl",
                [f"{name} {i}" for i in range(40)] + [f"needle {name}"],
            ),
            created_day=day,
        )
        for name, day in (("x", 3), ("y", 2), ("z", 1))
    ]
    manager = _manager(sessions)
    registry = _registry_for(manager)

    found: list[str] = []
    cursor: str | None = None
    while True:
        arguments: dict[str, Any] = {"query": "needle"}
        if cursor is not None:
            arguments["cursor"] = cursor
        result = await _search(registry, **arguments)
        found.extend(_contents(result))
        cursor = result.get("next_cursor")
        if cursor is None:
            break
        # Recency order moves with activity, including the caller's own session.
        manager.list.return_value = [sessions[2], sessions[0], sessions[1]]

    assert found == ["needle x", "needle y", "needle z"]


@pytest.mark.asyncio
async def test_a_foreign_session_is_refused_before_any_render(
    tmp_path: Path, work: _WorkCounter
) -> None:
    path = _transcript(tmp_path / "foreign.jsonl", ["secret needle"])
    registry = _registry([_session("sess-foreign", path, foreign=True)])

    result = await _search(registry, query="needle", session_id="sess-foreign")

    assert result["success"] is False
    assert "another machine" in result["error"]
    assert work.groups_rendered == 0


@pytest.mark.asyncio
async def test_a_malformed_cursor_is_rejected(tmp_path: Path) -> None:
    path = _transcript(tmp_path / "t.jsonl", ["needle"])
    registry = _registry([_session("sess-1", path)])

    result = await _search(registry, query="needle", session_id="sess-1", cursor="not-a-cursor")

    assert result["success"] is False
    assert "cursor" in result["error"]
