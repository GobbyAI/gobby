from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.idle_check_handler import IdleCheckHandler
from gobby.agents.watchdog import WatchdogReaderRegistry
from gobby.agents.watchdog.transcript_resolver import WatchdogTranscriptResolver
from gobby.storage.session_models import Session

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.agents.watchdog.transcript_resolver.get_machine_id",
        lambda: LOCAL_MACHINE_ID,
    )
    monkeypatch.setattr(
        "gobby.agents.idle_check_handler.get_machine_id",
        lambda: LOCAL_MACHINE_ID,
    )


def _handler(
    *,
    idle_check_enabled: bool = True,
    run_db: AsyncMock | None = None,
) -> IdleCheckHandler:
    config = SimpleNamespace(
        idle_check_enabled=idle_check_enabled,
        idle_timeout_seconds=60,
        idle_reprompt_delay_seconds=60,
        max_reprompt_attempts=2,
    )
    return IdleCheckHandler(
        agent_run_manager=MagicMock(),
        db=MagicMock(),
        get_session_manager=lambda: MagicMock(),
        tmux=MagicMock(),
        idle_detector=MagicMock(),
        prompt_detector=MagicMock(),
        stall_classifier=MagicMock(),
        watchdog_readers=WatchdogReaderRegistry(),
        cleanup_handler=MagicMock(),
        tmux_config=config,
        run_db=run_db,
    )


def _session(
    *,
    transcript_path: str | None,
    source: str = "codex",
    external_id: str = "external-1",
) -> Session:
    return cast(
        Session,
        SimpleNamespace(
            id="session-1",
            machine_id=LOCAL_MACHINE_ID,
            transcript_path=transcript_path,
            source=source,
            external_id=external_id,
            updated_at="2024-01-01T00:00:00+00:00",
        ),
    )


@pytest.mark.asyncio
async def test_resolve_transcript_path_accepts_valid_stored_file(tmp_path: Path) -> None:
    transcript = tmp_path / "stored.jsonl"
    transcript.write_text("{}\n")
    resolver = WatchdogTranscriptResolver()
    session = _session(transcript_path=str(transcript))

    with patch(
        "gobby.agents.watchdog.transcript_resolver.find_transcript_on_disk"
    ) as mock_discover:
        resolved = await resolver.resolve(session, run_id="run-1")

    assert resolved == str(transcript)
    mock_discover.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_transcript_path_rediscovers_after_stat_race(tmp_path: Path) -> None:
    stored = tmp_path / "stored.jsonl"
    discovered = tmp_path / "discovered.jsonl"
    stored.write_text("{}\n")
    discovered.write_text("{}\n")
    resolver = WatchdogTranscriptResolver()
    session = _session(transcript_path=str(stored))

    with (
        patch(
            "gobby.agents.watchdog.transcript_resolver.os.path.getmtime",
            side_effect=OSError,
        ),
        patch(
            "gobby.agents.watchdog.transcript_resolver.find_transcript_on_disk",
            return_value=str(discovered),
        ) as mock_discover,
    ):
        resolved = await resolver.resolve(session, run_id="run-1")

    assert resolved == str(discovered)
    mock_discover.assert_called_once_with(
        "codex",
        "external-1",
        owner_machine_id=LOCAL_MACHINE_ID,
        local_machine_id=LOCAL_MACHINE_ID,
        caller_context="recovery",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_kind", ["missing", "sentinel", "directory"])
async def test_resolve_transcript_path_discovers_for_invalid_stored_path(
    tmp_path: Path,
    stored_kind: str,
) -> None:
    discovered = tmp_path / "discovered.jsonl"
    discovered.write_text("{}\n")
    directory = tmp_path / "transcript-dir"
    directory.mkdir()
    stored_paths = {
        "missing": str(tmp_path / "removed.jsonl"),
        "sentinel": "missing_transcript",
        "directory": str(directory),
    }
    resolver = WatchdogTranscriptResolver()
    session = _session(transcript_path=stored_paths[stored_kind])
    updated_at = session.updated_at

    with patch(
        "gobby.agents.watchdog.transcript_resolver.find_transcript_on_disk",
        return_value=str(discovered),
    ) as mock_discover:
        resolved = await resolver.resolve(session, run_id="run-1")

    assert resolved == str(discovered)
    assert session.transcript_path == stored_paths[stored_kind]
    assert session.updated_at == updated_at
    mock_discover.assert_called_once_with(
        "codex",
        "external-1",
        owner_machine_id=LOCAL_MACHINE_ID,
        local_machine_id=LOCAL_MACHINE_ID,
        caller_context="recovery",
    )


@pytest.mark.asyncio
async def test_resolve_transcript_path_revalidates_cached_file(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text("{}\n")
    second.write_text("{}\n")
    resolver = WatchdogTranscriptResolver()
    session = _session(transcript_path=None)

    with patch(
        "gobby.agents.watchdog.transcript_resolver.find_transcript_on_disk",
        side_effect=[str(first), str(second)],
    ) as mock_discover:
        assert await resolver.resolve(session, run_id="run-1") == str(first)
        os.unlink(first)
        assert await resolver.resolve(session, run_id="run-1") == str(second)

    assert mock_discover.call_count == 2


@pytest.mark.asyncio
async def test_resolve_transcript_path_rediscovers_when_cache_predates_session_update(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text("{}\n")
    second.write_text("{}\n")
    resolver = WatchdogTranscriptResolver()
    session = _session(transcript_path=str(first))

    with patch(
        "gobby.agents.watchdog.transcript_resolver.find_transcript_on_disk",
        return_value=str(second),
    ) as mock_discover:
        assert await resolver.resolve(session, run_id="run-1") == str(first)
        session.updated_at = datetime(2100, 1, 1, tzinfo=UTC)
        assert await resolver.resolve(session, run_id="run-1") == str(second)

    mock_discover.assert_called_once_with(
        "codex",
        "external-1",
        owner_machine_id=LOCAL_MACHINE_ID,
        local_machine_id=LOCAL_MACHINE_ID,
        caller_context="recovery",
    )


@pytest.mark.asyncio
async def test_resolve_transcript_path_invalidates_cache_on_identity_change(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text("{}\n")
    second.write_text("{}\n")
    resolver = WatchdogTranscriptResolver()
    session = _session(transcript_path=None, external_id="external-1")

    with patch(
        "gobby.agents.watchdog.transcript_resolver.find_transcript_on_disk",
        side_effect=[str(first), str(second)],
    ):
        assert await resolver.resolve(session, run_id="run-1") == str(first)
        session.external_id = "external-2"
        assert await resolver.resolve(session, run_id="run-1") == str(second)

    assert resolver._path_cache == {("run-1", "codex", "external-2"): str(second)}


@pytest.mark.asyncio
async def test_check_idle_agents_prunes_transcript_cache_with_capacity_state() -> None:
    active_run = SimpleNamespace(id="run-active")
    run_db = AsyncMock(return_value=[active_run])
    handler = _handler(run_db=run_db)
    handler._transcript_resolver._path_cache = {
        ("run-active", "codex", "external-1"): "/active.jsonl",
        ("run-stale", "codex", "external-2"): "/stale.jsonl",
    }
    handler._recovery._capacity_recovery = {
        "run-active": MagicMock(),
        "run-stale": MagicMock(),
    }

    with patch.object(handler, "_handle_idle_check", new_callable=AsyncMock, return_value=0):
        await handler.check_idle_agents()

    assert set(handler._transcript_resolver._path_cache) == {("run-active", "codex", "external-1")}
    assert set(handler._recovery._capacity_recovery) == {"run-active"}


@pytest.mark.asyncio
async def test_disabled_idle_checks_clear_transcript_cache_with_capacity_state() -> None:
    handler = _handler(idle_check_enabled=False)
    handler._transcript_resolver._path_cache = {
        ("run-1", "codex", "external-1"): "/transcript.jsonl"
    }
    handler._recovery._capacity_recovery = {"run-1": MagicMock()}
    handler._recovery._completed_turn_recovery = {"run-1": MagicMock()}

    assert await handler.check_idle_agents() == 0

    assert handler._transcript_resolver._path_cache == {}
    assert handler._recovery._capacity_recovery == {}
    assert handler._recovery._completed_turn_recovery == {}
