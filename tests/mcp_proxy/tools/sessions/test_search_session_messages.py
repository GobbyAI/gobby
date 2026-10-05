"""search_session_messages bounds its work per call and resumes by cursor (#23117)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
from gobby.storage.unmodeled_observations import UnmodeledObservationStore

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
    session_id: str,
    path: Path,
    *,
    created_day: int = 1,
    created_minute: int = 0,
    foreign: bool = False,
) -> SimpleNamespace:
    created_at = datetime(2026, 9, created_day, tzinfo=UTC) + timedelta(minutes=created_minute)
    return SimpleNamespace(
        id=session_id,
        external_id=f"no-archive-{session_id}",
        source="claude",
        transcript_path=str(path),
        machine_id=LOCAL_MACHINE_ID,
        created_at=created_at,
        updated_at=created_at,
        foreign=foreign,
    )


def _manager(sessions: list[SimpleNamespace]) -> MagicMock:
    """Session manager fake with the storage layer's list orderings."""
    by_id = {session.id: session for session in sessions}
    manager = MagicMock()
    manager.resolve_session_reference.side_effect = lambda ref, _project_id=None: ref
    manager.get.side_effect = by_id.get

    def list_recent(*, limit: int, **_filters: Any) -> list[SimpleNamespace]:
        return sorted(sessions, key=lambda s: (s.updated_at, s.id), reverse=True)[:limit]

    def list_newest_created(
        *,
        limit: int,
        from_created_at: datetime | None = None,
        from_id: str | None = None,
        **_filters: Any,
    ) -> list[SimpleNamespace]:
        ordered = sorted(sessions, key=lambda s: (s.created_at, s.id), reverse=True)
        if from_created_at is not None and from_id is not None:
            ordered = [s for s in ordered if (s.created_at, s.id) <= (from_created_at, from_id)]
        return ordered[:limit]

    manager.list.side_effect = list_recent
    manager.list_newest_created.side_effect = list_newest_created
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


@pytest.mark.parametrize("limit,full_content", [(30, True), (5, False)])
@pytest.mark.parametrize("store_failure", [False, True])
async def test_tool_heavy_search_batches_telemetry_and_preserves_cursor_matches(
    tmp_path: Path, limit: int, full_content: bool, store_failure: bool
) -> None:
    path = tmp_path / "tool-heavy.jsonl"
    lines: list[dict[str, Any]] = []
    for i in range(120):
        lines.extend(
            [
                {"type": "user", "message": {"role": "user", "content": f"needle {i}"}},
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"call-{i}",
                                "name": "novel_tool_23408",
                                "input": {"index": i},
                            },
                        ],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": f"call-{i}", "content": "done"},
                        ],
                    },
                },
            ]
        )
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    manager = _manager([_session("tool-heavy", path)])
    store = MagicMock(spec=UnmodeledObservationStore)
    if store_failure:
        store.record_many.side_effect = RuntimeError("telemetry unavailable")
    registry = InternalToolRegistry(name="gobby-sessions", description="test")
    register_message_tools(registry, manager, TranscriptReader(manager, observation_store=store))
    found: list[str] = []
    cursor = None
    calls = 0
    while True:
        result = await _search(
            registry,
            query="needle",
            session_id="tool-heavy",
            limit=limit,
            full_content=full_content,
            cursor=cursor,
        )
        assert result["success"] is True
        assert len(result["results"]) <= limit
        found.extend(_contents(result))
        calls += 1
        cursor = result.get("next_cursor")
        if not cursor:
            break
        assert calls <= 120
    assert found == [f"needle {i}" for i in range(120)]
    assert store.record.call_count == 0
    assert 0 < store.record_many.call_count <= 2 * calls


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
        sessions[2].updated_at = datetime.now(UTC)

    assert found == ["needle x", "needle y", "needle z"]


@pytest.mark.asyncio
async def test_cursor_pages_visit_every_session_once_beyond_the_listing_cap_across_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_messages, "SEARCH_GROUP_BUDGET", 30)
    total = _messages.MAX_SEARCH_SESSIONS + 20
    sessions = [
        _session(
            f"sess-{i:03d}",
            _transcript(tmp_path / f"{i:03d}.jsonl", [f"needle {i:03d}"]),
            created_minute=i,
        )
        for i in range(total)
    ]
    registry = _registry_for(_manager(sessions))

    found: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        arguments: dict[str, Any] = {"query": "needle"}
        if cursor is not None:
            arguments["cursor"] = cursor
        result = await _search(registry, **arguments)
        found.extend(_contents(result))
        cursor = result.get("next_cursor")
        if cursor is None:
            break
        pages += 1
        # Writes between pages make the oldest sessions the most recently updated.
        for session in sessions[: pages * 15]:
            session.updated_at = datetime.now(UTC)

    assert found == [f"needle {i:03d}" for i in reversed(range(total))]


@pytest.mark.asyncio
async def test_an_explicit_session_refuses_another_sessions_cursor(
    tmp_path: Path, work: _WorkCounter
) -> None:
    first = _transcript(tmp_path / "a.jsonl", ["needle a1", "needle a2"])
    second = _transcript(tmp_path / "b.jsonl", ["needle b1"])
    registry = _registry([_session("sess-a", first), _session("sess-b", second)])
    page = await _search(registry, query="needle", session_id="sess-a", limit=1)
    rendered = work.groups_rendered

    refused = await _search(
        registry, query="needle", session_id="sess-b", cursor=page["next_cursor"]
    )

    assert refused == {"success": False, "error": "cursor belongs to another session"}
    assert work.groups_rendered == rendered


@pytest.mark.asyncio
async def test_a_cursor_for_a_vanished_session_is_rejected(tmp_path: Path) -> None:
    path = _transcript(tmp_path / "a.jsonl", ["needle 1", "needle 2"])
    manager = _manager([_session("sess-a", path)])
    registry = _registry_for(manager)
    page = await _search(registry, query="needle", limit=1)
    manager.get.side_effect = lambda _session_id: None

    result = await _search(registry, query="needle", cursor=page["next_cursor"])

    assert result == {"success": False, "error": "cursor session no longer exists"}


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
