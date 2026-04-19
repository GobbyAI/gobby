"""Tests for ConductorManager tick-based orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.conductor.manager import CONDUCTOR_SYSTEM_PROMPT, ConductorManager
from gobby.config.conductor import ConductorConfig

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000001"


def _make_manager(
    config: ConductorConfig | None = None,
) -> tuple[ConductorManager, MagicMock]:
    """Create a ConductorManager with a mocked session_manager."""
    cfg = config or ConductorConfig(enabled=True, model="haiku")
    session_manager = MagicMock()
    mock_session = MagicMock()
    mock_session.id = "db-session-id"
    mock_session.seq_num = 1
    session_manager.register.return_value = mock_session

    manager = ConductorManager(
        project_id=PROJECT_ID,
        project_path="/tmp/test-project",
        session_manager=session_manager,
        config=cfg,
    )
    return manager, session_manager


def _mock_chat_session(is_connected: bool = True) -> MagicMock:
    """Create a mock ChatSession."""
    from gobby.llm.claude_models import DoneEvent, TextChunk

    session = MagicMock()
    session.is_connected = is_connected
    session.start = AsyncMock()
    session.stop = AsyncMock()

    async def mock_send_message(content: str):
        yield TextChunk(content="No action needed")
        yield DoneEvent(
            tool_calls_count=0,
            input_tokens=100,
            output_tokens=20,
            duration_ms=500,
        )

    session.send_message = mock_send_message
    return session


@pytest.mark.asyncio
async def test_handle_tick_creates_session_on_first_call() -> None:
    """First tick creates a ChatSession with correct project_id and model."""
    manager, session_manager = _make_manager()
    mock_session = _mock_chat_session()

    with patch("gobby.servers.chat_session.ChatSession", return_value=mock_session) as cls:
        job = MagicMock()
        result = await manager(job)

        # ChatSession created with correct args
        cls.assert_called_once_with(
            conversation_id=f"conductor-{PROJECT_ID}",
            provider="claude",
            project_id=PROJECT_ID,
            project_path="/tmp/test-project",
        )
        assert mock_session.system_prompt_override == CONDUCTOR_SYSTEM_PROMPT
        mock_session.start.assert_called_once_with(model="haiku")
        session_manager.register.assert_called_once()
        assert "No action needed" in result


@pytest.mark.asyncio
async def test_handle_tick_reuses_existing_session() -> None:
    """Second tick reuses the same session."""
    manager, _ = _make_manager()
    mock_session = _mock_chat_session()

    with patch("gobby.servers.chat_session.ChatSession", return_value=mock_session) as cls:
        job = MagicMock()
        await manager(job)
        await manager(job)

        # Only created once
        cls.assert_called_once()


@pytest.mark.asyncio
async def test_handle_tick_skip_if_busy() -> None:
    """Returns skip message when _busy=True and skip_if_busy enabled."""
    manager, _ = _make_manager(ConductorConfig(enabled=True, skip_if_busy=True))
    manager._busy = True

    job = MagicMock()
    result = await manager(job)
    assert "busy" in result.lower()
    assert "skipping" in result.lower()


@pytest.mark.asyncio
async def test_handle_tick_no_skip_when_disabled() -> None:
    """When skip_if_busy=False, tick proceeds even if busy."""
    manager, _ = _make_manager(ConductorConfig(enabled=True, skip_if_busy=False))
    mock_session = _mock_chat_session()

    with patch("gobby.servers.chat_session.ChatSession", return_value=mock_session):
        # Set busy but skip_if_busy=False — should still process
        # Note: _busy is set to True inside _handle_tick, then reset in finally
        job = MagicMock()
        result = await manager(job)
        assert "Conductor:" in result


@pytest.mark.asyncio
async def test_init_rejects_non_claude_provider() -> None:
    """Conductor should fail fast instead of waiting for the first tick."""
    with pytest.raises(RuntimeError, match="provider 'codex' is not supported"):
        _make_manager(ConductorConfig(enabled=True, provider="codex", model="gpt-5"))


@pytest.mark.asyncio
async def test_handle_tick_still_rejects_non_claude_provider_after_init() -> None:
    """The tick path keeps the defensive provider guard in place."""
    manager, _ = _make_manager()
    manager._config.provider = "codex"

    job = MagicMock()
    result = await manager(job)

    assert "provider 'codex' is not supported" in result


@pytest.mark.asyncio
async def test_handle_tick_recreates_dead_session() -> None:
    """If is_connected=False, creates a new session."""
    manager, _ = _make_manager()
    dead_session = _mock_chat_session(is_connected=False)
    new_session = _mock_chat_session(is_connected=True)

    call_count = 0

    def make_session(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return dead_session
        return new_session

    with patch("gobby.servers.chat_session.ChatSession", side_effect=make_session):
        job = MagicMock()
        # First call creates dead_session, starts it (becomes "connected" for the tick)
        await manager(job)
        # Simulate it becoming disconnected
        manager._session = dead_session  # type: ignore[assignment]
        # Second call should create a new session
        await manager(job)
        assert call_count == 2


@pytest.mark.asyncio
async def test_idle_timeout_destroys_session() -> None:
    """Session torn down and recreated if idle exceeds timeout."""
    config = ConductorConfig(enabled=True, idle_timeout_seconds=300)
    manager, _ = _make_manager(config)
    mock_session = _mock_chat_session()

    with patch("gobby.servers.chat_session.ChatSession", return_value=mock_session) as cls:
        job = MagicMock()
        await manager(job)

        # Simulate idle timeout: set last_activity to 6 minutes ago
        manager._last_activity = datetime.now(UTC) - timedelta(minutes=6)

        # Next tick should tear down and recreate
        new_session = _mock_chat_session()
        cls.return_value = new_session
        await manager(job)

        # Old session stopped, new one created
        mock_session.stop.assert_called_once()
        assert cls.call_count == 2


@pytest.mark.asyncio
async def test_handle_tick_error_destroys_session() -> None:
    """send_message raising destroys session; next tick recreates."""
    manager, _ = _make_manager()
    error_session = MagicMock()
    error_session.is_connected = True
    error_session.start = AsyncMock()
    error_session.stop = AsyncMock()

    async def failing_send(content: str):
        if False:
            yield
        raise RuntimeError("SDK connection lost")

    error_session.send_message = failing_send

    with patch("gobby.servers.chat_session.ChatSession", return_value=error_session):
        job = MagicMock()
        result = await manager(job)

        assert "failed" in result.lower()
        error_session.stop.assert_called_once()
        assert manager._session is None


@pytest.mark.asyncio
async def test_shutdown() -> None:
    """shutdown() calls session.stop()."""
    manager, _ = _make_manager()
    mock_session = _mock_chat_session()

    with patch("gobby.servers.chat_session.ChatSession", return_value=mock_session):
        job = MagicMock()
        await manager(job)
        await manager.shutdown()

        mock_session.stop.assert_called_once()
        assert manager._session is None


@pytest.mark.asyncio
async def test_shutdown_no_session() -> None:
    """shutdown() is safe when no session exists."""
    manager, _ = _make_manager()
    await manager.shutdown()  # Should not raise


@pytest.mark.asyncio
async def test_cron_handler_interface() -> None:
    """__call__ works as CronHandler (receives CronJob, returns str)."""
    manager, _ = _make_manager()
    mock_session = _mock_chat_session()

    with patch("gobby.servers.chat_session.ChatSession", return_value=mock_session):
        job = MagicMock()
        job.name = "gobby:conductor-tick"
        result = await manager(job)

        assert isinstance(result, str)
        assert len(result) > 0


@pytest.mark.asyncio
async def test_busy_flag_reset_on_error() -> None:
    """_busy flag is always reset, even on error."""
    manager, _ = _make_manager(ConductorConfig(enabled=True, skip_if_busy=False))
    error_session = MagicMock()
    error_session.is_connected = True
    error_session.start = AsyncMock()
    error_session.stop = AsyncMock()

    async def failing_send(content: str):
        if False:
            yield
        raise RuntimeError("boom")

    error_session.send_message = failing_send

    with patch("gobby.servers.chat_session.ChatSession", return_value=error_session):
        job = MagicMock()
        await manager(job)
        assert manager._busy is False
