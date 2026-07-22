from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.idle_check_handler import IdleCheckHandler
from gobby.agents.watchdog import WatchdogReaderRegistry


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
) -> SimpleNamespace:
    return SimpleNamespace(
        transcript_path=transcript_path,
        source=source,
        external_id=external_id,
        updated_at="2024-01-01T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_resolve_transcript_path_accepts_valid_stored_file(tmp_path: Path) -> None:
    transcript = tmp_path / "stored.jsonl"
    transcript.write_text("{}\n")
    handler = _handler()
    session = _session(transcript_path=str(transcript))

    with patch("gobby.agents.idle_check_handler._find_transcript_on_disk") as mock_discover:
        resolved = await handler._resolve_transcript_path(session, run_id="run-1")

    assert resolved == str(transcript)
    mock_discover.assert_not_called()


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
    handler = _handler()
    session = _session(transcript_path=stored_paths[stored_kind])
    updated_at = session.updated_at

    with patch(
        "gobby.agents.idle_check_handler._find_transcript_on_disk",
        return_value=str(discovered),
    ) as mock_discover:
        resolved = await handler._resolve_transcript_path(session, run_id="run-1")

    assert resolved == str(discovered)
    assert session.transcript_path == stored_paths[stored_kind]
    assert session.updated_at == updated_at
    mock_discover.assert_called_once_with("codex", "external-1")


@pytest.mark.asyncio
async def test_resolve_transcript_path_revalidates_cached_file(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text("{}\n")
    second.write_text("{}\n")
    handler = _handler()
    session = _session(transcript_path=None)

    with patch(
        "gobby.agents.idle_check_handler._find_transcript_on_disk",
        side_effect=[str(first), str(second)],
    ) as mock_discover:
        assert await handler._resolve_transcript_path(session, run_id="run-1") == str(first)
        os.unlink(first)
        assert await handler._resolve_transcript_path(session, run_id="run-1") == str(second)

    assert mock_discover.call_count == 2


@pytest.mark.asyncio
async def test_resolve_transcript_path_invalidates_cache_on_identity_change(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text("{}\n")
    second.write_text("{}\n")
    handler = _handler()
    session = _session(transcript_path=None, external_id="external-1")

    with patch(
        "gobby.agents.idle_check_handler._find_transcript_on_disk",
        side_effect=[str(first), str(second)],
    ):
        assert await handler._resolve_transcript_path(session, run_id="run-1") == str(first)
        session.external_id = "external-2"
        assert await handler._resolve_transcript_path(session, run_id="run-1") == str(second)

    assert handler._transcript_path_cache == {("run-1", "codex", "external-2"): str(second)}


@pytest.mark.asyncio
async def test_check_idle_agents_prunes_transcript_cache_with_capacity_state() -> None:
    active_run = SimpleNamespace(id="run-active")
    run_db = AsyncMock(return_value=[active_run])
    handler = _handler(run_db=run_db)
    handler._transcript_path_cache = {
        ("run-active", "codex", "external-1"): "/active.jsonl",
        ("run-stale", "codex", "external-2"): "/stale.jsonl",
    }
    handler._capacity_recovery = {
        "run-active": MagicMock(),
        "run-stale": MagicMock(),
    }

    with patch.object(handler, "_handle_idle_check", new_callable=AsyncMock, return_value=0):
        await handler.check_idle_agents()

    assert set(handler._transcript_path_cache) == {("run-active", "codex", "external-1")}
    assert set(handler._capacity_recovery) == {"run-active"}


@pytest.mark.asyncio
async def test_disabled_idle_checks_clear_transcript_cache_with_capacity_state() -> None:
    handler = _handler(idle_check_enabled=False)
    handler._transcript_path_cache = {("run-1", "codex", "external-1"): "/transcript.jsonl"}
    handler._capacity_recovery = {"run-1": MagicMock()}

    assert await handler.check_idle_agents() == 0

    assert handler._transcript_path_cache == {}
    assert handler._capacity_recovery == {}
