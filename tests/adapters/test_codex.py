"""Comprehensive tests for Codex CLI adapter.

Tests cover:
1. CodexAppServerClient - subprocess and JSON-RPC management
2. CodexAdapter - event translation from app-server
3. CodexNotifyAdapter - notify hook handling
4. Data types and utilities
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter, _get_daemon_machine_id
from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.adapters.codex_impl.hooks_adapter import CodexNotifyAdapter
from gobby.adapters.codex_impl.types import (
    CodexConnectionState,
    CodexItem,
    CodexThread,
    CodexTurn,
)
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.llm.sdk_utils import ADDITIONAL_CONTEXT_LIMIT
from tests._timing import wait_forever

pytestmark = pytest.mark.unit

# =============================================================================
# Data Types Tests
# =============================================================================


class TestCodexConnectionState:
    """Tests for CodexConnectionState enum."""

    def test_connection_states(self) -> None:
        """All connection states are defined."""
        assert CodexConnectionState.DISCONNECTED.value == "disconnected"
        assert CodexConnectionState.CONNECTING.value == "connecting"
        assert CodexConnectionState.CONNECTED.value == "connected"
        assert CodexConnectionState.ERROR.value == "error"


class TestCodexThread:
    """Tests for CodexThread dataclass."""

    def test_create_minimal(self) -> None:
        """Create thread with only required field."""
        thread = CodexThread(id="thr-123")

        assert thread.id == "thr-123"
        assert thread.preview == ""
        assert thread.model_provider == "openai"
        assert thread.created_at == 0

    def test_create_full(self) -> None:
        """Create thread with all fields."""
        thread = CodexThread(
            id="thr-456",
            preview="Help me refactor",
            model_provider="anthropic",
            created_at=1704067200,
        )

        assert thread.id == "thr-456"
        assert thread.preview == "Help me refactor"
        assert thread.model_provider == "anthropic"
        assert thread.created_at == 1704067200


class TestCodexTurn:
    """Tests for CodexTurn dataclass."""

    def test_create_minimal(self) -> None:
        """Create turn with required fields."""
        turn = CodexTurn(id="turn-1", thread_id="thr-1")

        assert turn.id == "turn-1"
        assert turn.thread_id == "thr-1"
        assert turn.status == "pending"
        assert turn.items == []
        assert turn.error is None
        assert turn.usage is None

    def test_create_full(self) -> None:
        """Create turn with all fields."""
        turn = CodexTurn(
            id="turn-2",
            thread_id="thr-2",
            status="completed",
            items=[{"type": "message", "text": "Done"}],
            error="Some error",
            usage={"input_tokens": 100, "output_tokens": 50},
        )

        assert turn.status == "completed"
        assert len(turn.items) == 1
        assert turn.error == "Some error"
        assert turn.usage["input_tokens"] == 100


class TestCodexItem:
    """Tests for CodexItem dataclass."""

    def test_create_minimal(self) -> None:
        """Create item with required fields."""
        item = CodexItem(id="item-1", type="reasoning")

        assert item.id == "item-1"
        assert item.type == "reasoning"
        assert item.content == ""
        assert item.status == "pending"
        assert item.metadata == {}

    def test_create_full(self) -> None:
        """Create item with all fields."""
        item = CodexItem(
            id="item-2",
            type="agent_message",
            content="I'll help you with that",
            status="completed",
            metadata={"model": "gpt-4"},
        )

        assert item.content == "I'll help you with that"
        assert item.status == "completed"
        assert item.metadata["model"] == "gpt-4"


class TestGetMachineId:
    """Tests for _get_daemon_machine_id utility."""

    def test_returns_string(self) -> None:
        """Returns a string machine ID."""
        machine_id = _get_daemon_machine_id()
        assert isinstance(machine_id, str)
        assert len(machine_id) > 0

    @patch("gobby.utils.machine_id.get_machine_id")
    def test_returns_stable_id(self, mock_get_machine_id) -> None:
        """Returns stable ID from utils.machine_id."""
        mock_get_machine_id.return_value = "test-machine-id-12345"

        id1 = _get_daemon_machine_id()
        id2 = _get_daemon_machine_id()

        # Same machine should produce same ID
        assert id1 == id2
        assert id1 == "test-machine-id-12345"

    @patch("gobby.utils.machine_id.get_machine_id")
    def test_fallback_when_no_hostname(self, mock_get_machine_id) -> None:
        """Returns valid ID from utils.machine_id (may be UUID or machineid format)."""
        # machineid returns 32-char hex, uuid4 returns 36-char UUID
        mock_get_machine_id.return_value = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"

        machine_id = _get_daemon_machine_id()
        assert isinstance(machine_id, str)
        # Accept both machineid format (32 chars) and UUID format (36 chars)
        assert len(machine_id) >= 32


# =============================================================================
# CodexAppServerClient Tests
# =============================================================================


class TestCodexAppServerClientInit:
    """Tests for CodexAppServerClient initialization."""

    def test_default_init(self) -> None:
        """Default initialization."""
        client = CodexAppServerClient()

        assert client._codex_command == "codex"
        assert client._on_notification is None
        assert client._process is None
        assert client.state == CodexConnectionState.DISCONNECTED
        assert client.is_connected is False
        assert client._thread_cwds == {}

    def test_custom_command(self) -> None:
        """Initialize with custom codex command."""
        client = CodexAppServerClient(codex_command="/custom/codex")
        assert client._codex_command == "/custom/codex"

    def test_with_notification_handler(self) -> None:
        """Initialize with notification handler."""

        def handler(method: str, params: dict) -> None:
            pass

        client = CodexAppServerClient(on_notification=handler)
        assert client._on_notification is handler


class TestCodexAppServerClientProperties:
    """Tests for CodexAppServerClient properties."""

    def test_state_property(self) -> None:
        """State property returns current state."""
        client = CodexAppServerClient()
        assert client.state == CodexConnectionState.DISCONNECTED

    def test_is_connected_false_when_disconnected(self) -> None:
        """is_connected returns False when disconnected."""
        client = CodexAppServerClient()
        assert client.is_connected is False

    def test_is_connected_true_when_connected(self) -> None:
        """is_connected returns True when connected."""
        client = CodexAppServerClient()
        client._state = CodexConnectionState.CONNECTED
        assert client.is_connected is True


class TestCodexAppServerClientNotificationHandlers:
    """Tests for notification handler management."""

    def test_add_notification_handler(self) -> None:
        """Add a notification handler."""
        client = CodexAppServerClient()
        handler = MagicMock()

        client.add_notification_handler("turn/started", handler)

        assert "turn/started" in client._notification_handlers
        assert handler in client._notification_handlers["turn/started"]

    def test_add_multiple_handlers(self) -> None:
        """Add multiple handlers for same method."""
        client = CodexAppServerClient()
        handler1 = MagicMock()
        handler2 = MagicMock()

        client.add_notification_handler("turn/completed", handler1)
        client.add_notification_handler("turn/completed", handler2)

        assert len(client._notification_handlers["turn/completed"]) == 2

    def test_remove_notification_handler(self) -> None:
        """Remove a notification handler."""
        client = CodexAppServerClient()
        handler = MagicMock()

        client.add_notification_handler("item/completed", handler)
        client.remove_notification_handler("item/completed", handler)

        assert handler not in client._notification_handlers.get("item/completed", [])

    def test_remove_nonexistent_handler(self) -> None:
        """Remove handler that doesn't exist."""
        client = CodexAppServerClient()
        handler = MagicMock()

        client.remove_notification_handler("missing", handler)
        assert client._notification_handlers == {}


class TestCodexAppServerClientStart:
    """Tests for CodexAppServerClient.start()."""

    @pytest.mark.asyncio
    async def test_start_spawns_subprocess(self) -> None:
        """Start spawns codex app-server subprocess."""
        client = CodexAppServerClient()

        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_process.stderr = MagicMock()
        mock_process.poll.return_value = None

        # Mock the response for initialize request
        def mock_readline() -> str:
            return (
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"userAgent": "codex/1.0"}}) + "\n"
            )

        mock_process.stdout.readline = mock_readline

        with patch(
            "gobby.adapters.codex_impl.client.subprocess.Popen", return_value=mock_process
        ) as mock_popen:
            # Create a task that will complete quickly
            async def run_start() -> None:
                try:
                    await asyncio.wait_for(client.start(), timeout=0.5)
                except TimeoutError:
                    pass

            await run_start()

            mock_popen.assert_called_once()
            args = mock_popen.call_args
            assert args[0][0] == ["codex", "app-server"]
            assert args.kwargs["env"]["GOBBY_HOOKS_DISABLED"] == "1"

        await client.stop()

    @pytest.mark.asyncio
    async def test_start_appends_config_overrides_and_features(self) -> None:
        """Start forwards optional app-server config flags when configured."""
        client = CodexAppServerClient(
            config_overrides=("model='gpt-5.4'", "sandbox='workspace-write'"),
            enabled_features=("fast_mode",),
            disabled_features=("guardian_approval",),
        )

        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_process.stderr = MagicMock()
        mock_process.poll.return_value = None

        def mock_readline() -> str:
            return (
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"userAgent": "codex/1.0"}}) + "\n"
            )

        mock_process.stdout.readline = mock_readline

        with patch(
            "gobby.adapters.codex_impl.client.subprocess.Popen", return_value=mock_process
        ) as mock_popen:

            async def run_start() -> None:
                try:
                    await asyncio.wait_for(client.start(), timeout=0.5)
                except TimeoutError:
                    pass

            await run_start()

            args = mock_popen.call_args
            assert args[0][0] == [
                "codex",
                "app-server",
                "-c",
                "model='gpt-5.4'",
                "-c",
                "sandbox='workspace-write'",
                "--enable",
                "fast_mode",
                "--disable",
                "guardian_approval",
            ]
            assert args.kwargs["env"]["GOBBY_HOOKS_DISABLED"] == "1"

        await client.stop()

    @pytest.mark.asyncio
    async def test_start_when_already_connected(self) -> None:
        """Start returns early when already connected."""
        client = CodexAppServerClient()
        client._state = CodexConnectionState.CONNECTED

        await client.start()

        # State should remain connected
        assert client.state == CodexConnectionState.CONNECTED

    @pytest.mark.asyncio
    async def test_start_failure_sets_error_state(self) -> None:
        """Start sets error state on failure."""
        client = CodexAppServerClient()

        with patch(
            "gobby.adapters.codex_impl.client.subprocess.Popen",
            side_effect=OSError("Command not found"),
        ):
            with pytest.raises(RuntimeError, match="Failed to start"):
                await client.start()

        assert client.state == CodexConnectionState.DISCONNECTED


class TestCodexAppServerClientStop:
    """Tests for CodexAppServerClient.stop()."""

    @pytest.mark.asyncio
    async def test_stop_terminates_process(self):
        """Stop terminates the subprocess."""
        client = CodexAppServerClient()

        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.wait.return_value = 0
        client._process = mock_process

        await client.stop()

        mock_process.terminate.assert_called_once()
        assert client._process is None
        assert client.state == CodexConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_stop_cancels_reader_task(self):
        """Stop cancels the reader task."""
        client = CodexAppServerClient()

        # Create an actual asyncio task that we can cancel
        async def long_running():
            await wait_forever()

        mock_task = asyncio.create_task(long_running())
        client._reader_task = mock_task

        # Mock the process
        mock_process = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.wait.return_value = 0
        client._process = mock_process

        await client.stop()

        assert mock_task.cancelled()

    @pytest.mark.asyncio
    async def test_stop_cancels_pending_requests(self):
        """Stop cancels all pending requests."""
        client = CodexAppServerClient()

        future1 = asyncio.get_event_loop().create_future()
        future2 = asyncio.get_event_loop().create_future()
        client._pending_requests = {1: future1, 2: future2}

        await client.stop()

        assert future1.cancelled()
        assert future2.cancelled()
        assert client._pending_requests == {}


class TestCodexAppServerClientContextManager:
    """Tests for async context manager support."""

    @pytest.mark.asyncio
    async def test_context_manager_start_stop(self):
        """Context manager starts and stops client."""
        client = CodexAppServerClient()

        with patch.object(client, "start", new_callable=AsyncMock) as mock_start:
            with patch.object(client, "stop", new_callable=AsyncMock) as mock_stop:
                async with client:
                    mock_start.assert_called_once()
                    assert mock_start.call_count == 1
                    assert mock_start.call_args is not None

                mock_stop.assert_called_once()
                assert mock_stop.call_count == 1
                assert mock_stop.call_args is not None


class TestCodexAppServerClientRequestId:
    """Tests for request ID generation."""

    def test_next_request_id_increments(self) -> None:
        """Request ID increments with each call."""
        client = CodexAppServerClient()

        id1 = client._next_request_id()
        id2 = client._next_request_id()
        id3 = client._next_request_id()

        assert id1 == 1
        assert id2 == 2
        assert id3 == 3


class TestCodexAppServerClientSendRequest:
    """Tests for _send_request method."""

    @pytest.mark.asyncio
    async def test_send_request_not_connected(self):
        """send_request raises when not connected."""
        client = CodexAppServerClient()

        with pytest.raises(RuntimeError, match="Not connected"):
            await client._send_request("test", {})

    @pytest.mark.asyncio
    async def test_send_request_formats_jsonrpc(self):
        """send_request sends properly formatted JSON-RPC."""
        client = CodexAppServerClient()

        mock_stdin = MagicMock()
        written_lines = []
        mock_stdin.write = lambda x: written_lines.append(x)
        mock_stdin.flush = MagicMock()

        mock_process = MagicMock()
        mock_process.stdin = mock_stdin
        client._process = mock_process

        # Create a future that we'll resolve
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        future.set_result({"key": "value"})

        with patch.dict(client._pending_requests, {1: future}):
            # This should timeout but we want to check the written data
            try:
                await asyncio.wait_for(
                    client._send_request("test/method", {"arg": "val"}), timeout=0.1
                )
            except TimeoutError:
                pass  # Expected - we're testing request was written before timeout

        assert len(written_lines) > 0
        message = json.loads(written_lines[0].strip())
        assert message["jsonrpc"] == "2.0"
        assert message["method"] == "test/method"
        assert message["params"] == {"arg": "val"}
        assert "id" in message


class TestCodexAppServerClientSendNotification:
    """Tests for _send_notification method."""

    @pytest.mark.asyncio
    async def test_send_notification_not_connected(self):
        """send_notification raises when not connected."""
        client = CodexAppServerClient()

        with pytest.raises(RuntimeError, match="Not connected"):
            await client._send_notification("test", {})

    @pytest.mark.asyncio
    async def test_send_notification_formats_message(self):
        """send_notification sends proper notification format (no id)."""
        client = CodexAppServerClient()

        mock_stdin = MagicMock()
        written_lines = []
        mock_stdin.write = lambda x: written_lines.append(x)
        mock_stdin.flush = MagicMock()

        mock_process = MagicMock()
        mock_process.stdin = mock_stdin
        client._process = mock_process

        await client._send_notification("initialized", {})

        assert len(written_lines) == 1
        message = json.loads(written_lines[0].strip())
        assert message["jsonrpc"] == "2.0"
        assert message["method"] == "initialized"
        assert "id" not in message


class TestCodexAppServerClientThreadManagement:
    """Tests for thread management methods."""

    @pytest.mark.asyncio
    async def test_start_thread(self):
        """start_thread sends request and returns thread."""
        client = CodexAppServerClient()

        mock_result = {
            "thread": {
                "id": "thr-new",
                "preview": "",
                "modelProvider": "openai",
                "createdAt": 1704067200,
            }
        }

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value=mock_result
        ) as mock_send:
            thread = await client.start_thread(cwd="/project", model="gpt-4")

            mock_send.assert_called_once_with(
                "thread/start",
                {"cwd": "/project", "model": "gpt-4"},
            )

        assert thread.id == "thr-new"
        assert thread.model_provider == "openai"
        assert "thr-new" in client._threads
        assert client._thread_cwds["thr-new"] == "/project"

    @pytest.mark.asyncio
    async def test_resume_thread(self):
        """resume_thread sends request and returns thread."""
        client = CodexAppServerClient()

        mock_result = {
            "thread": {
                "id": "thr-existing",
                "preview": "Previous work",
                "modelProvider": "anthropic",
                "createdAt": 1704000000,
            }
        }

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value=mock_result
        ):
            thread = await client.resume_thread("thr-existing")

        assert thread.id == "thr-existing"
        assert thread.preview == "Previous work"
        assert "thr-existing" in client._threads

    @pytest.mark.asyncio
    async def test_list_threads(self):
        """list_threads returns paginated thread list."""
        client = CodexAppServerClient()

        mock_result = {
            "data": [
                {"id": "thr-1", "preview": "First", "modelProvider": "openai", "createdAt": 1000},
                {"id": "thr-2", "preview": "Second", "modelProvider": "openai", "createdAt": 2000},
            ],
            "nextCursor": "cursor-abc",
        }

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value=mock_result
        ) as mock_send:
            threads, cursor = await client.list_threads(cursor=None, limit=10)

            mock_send.assert_called_once_with("thread/list", {"limit": 10})

        assert len(threads) == 2
        assert threads[0].id == "thr-1"
        assert cursor == "cursor-abc"

    @pytest.mark.asyncio
    async def test_archive_thread(self):
        """archive_thread sends request and removes from cache."""
        client = CodexAppServerClient()
        client._threads["thr-delete"] = CodexThread(id="thr-delete")
        client._thread_cwds["thr-delete"] = "/project"

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value={}
        ) as mock_send:
            await client.archive_thread("thr-delete")

            mock_send.assert_called_once_with("thread/archive", {"threadId": "thr-delete"})

        assert "thr-delete" not in client._threads
        assert "thr-delete" not in client._thread_cwds

    @pytest.mark.asyncio
    async def test_list_models_follows_pagination(self) -> None:
        """list_models requests model/list until nextCursor is exhausted."""
        client = CodexAppServerClient()

        with patch.object(
            client,
            "_send_request",
            new_callable=AsyncMock,
            side_effect=[
                {
                    "data": [{"model": "gpt-5.4"}],
                    "nextCursor": "cursor-1",
                },
                {
                    "data": [{"model": "gpt-5.4-mini"}],
                },
            ],
        ) as mock_send:
            models = await client.list_models(limit=10, include_hidden=True)

        assert models == [{"model": "gpt-5.4"}, {"model": "gpt-5.4-mini"}]
        assert mock_send.call_args_list[0].args == (
            "model/list",
            {"limit": 10, "includeHidden": True},
        )
        assert mock_send.call_args_list[1].args == (
            "model/list",
            {"limit": 10, "includeHidden": True, "cursor": "cursor-1"},
        )


class TestCodexAppServerClientTurnManagement:
    """Tests for turn management methods."""

    @pytest.mark.asyncio
    async def test_start_turn(self):
        """start_turn sends request and returns turn."""
        client = CodexAppServerClient()

        mock_result = {
            "turn": {
                "id": "turn-new",
                "status": "inProgress",
                "items": [],
            }
        }

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value=mock_result
        ) as mock_send:
            turn = await client.start_turn("thr-1", "Help me refactor")

            call_args = mock_send.call_args
            assert call_args[0][0] == "turn/start"
            params = call_args[0][1]
            assert params["threadId"] == "thr-1"
            assert params["input"][0]["type"] == "text"
            assert params["input"][0]["text"] == "Help me refactor"

        assert turn.id == "turn-new"
        assert turn.thread_id == "thr-1"
        assert turn.status == "inProgress"

    @pytest.mark.asyncio
    async def test_start_turn_with_images(self):
        """start_turn handles image inputs."""
        client = CodexAppServerClient()

        mock_result = {"turn": {"id": "turn-img", "status": "inProgress", "items": []}}

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value=mock_result
        ) as mock_send:
            await client.start_turn(
                "thr-1",
                "What's in this image?",
                images=["https://example.com/img.png", "/local/path.jpg"],
            )

            params = mock_send.call_args[0][1]
            assert len(params["input"]) == 3
            assert params["input"][1]["type"] == "image"
            assert params["input"][1]["url"] == "https://example.com/img.png"
            assert params["input"][2]["type"] == "localImage"
            assert params["input"][2]["path"] == "/local/path.jpg"

    @pytest.mark.asyncio
    async def test_start_turn_with_effort(self):
        """start_turn sends effort for per-turn reasoning overrides."""
        client = CodexAppServerClient()

        mock_result = {
            "turn": {
                "id": "turn-effort",
                "status": "inProgress",
                "items": [],
            }
        }

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value=mock_result
        ) as mock_send:
            await client.start_turn("thr-1", "Help me refactor", effort="xhigh")

            params = mock_send.call_args[0][1]
            assert params["effort"] == "xhigh"
            assert "reasoningEffort" not in params

    @pytest.mark.asyncio
    async def test_start_turn_maps_legacy_reasoning_effort_override(self):
        """start_turn keeps legacy callers working by mapping reasoningEffort."""
        client = CodexAppServerClient()

        mock_result = {
            "turn": {
                "id": "turn-legacy-effort",
                "status": "inProgress",
                "items": [],
            }
        }

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value=mock_result
        ) as mock_send:
            await client.start_turn("thr-1", "Help me refactor", reasoningEffort="high")

            params = mock_send.call_args[0][1]
            assert params["effort"] == "high"
            assert "reasoningEffort" not in params

    @pytest.mark.asyncio
    async def test_interrupt_turn(self):
        """interrupt_turn sends request."""
        client = CodexAppServerClient()

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value={}
        ) as mock_send:
            await client.interrupt_turn("thr-1", "turn-1")

            mock_send.assert_called_once_with(
                "turn/interrupt",
                {"threadId": "thr-1", "turnId": "turn-1"},
            )
            assert mock_send.call_count == 1
            assert mock_send.call_args is not None


class TestCodexAppServerClientRunTurn:
    """Tests for run_turn streaming method."""

    @pytest.mark.asyncio
    async def test_run_turn_yields_events(self):
        """run_turn yields streaming events."""
        client = CodexAppServerClient()

        mock_result = {"turn": {"id": "turn-stream", "status": "inProgress", "items": []}}

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value=mock_result
        ):
            events = []
            # Simulate notification that ends the turn
            client._notification_handlers["turn/completed"] = []

            async def collect_events():
                async for event in client.run_turn("thr-1", "Test"):
                    events.append(event)
                    if event["type"] == "turn/created":
                        # Simulate completion
                        for handler in client._notification_handlers.get("turn/completed", []):
                            handler(
                                "turn/completed",
                                {"turn": {"id": "turn-stream", "status": "completed"}},
                            )
                        break

            await collect_events()

            assert len(events) >= 1
            assert events[0]["type"] == "turn/created"


class TestCodexAppServerClientAuthentication:
    """Tests for authentication methods."""

    @pytest.mark.asyncio
    async def test_login_with_api_key(self):
        """login_with_api_key sends request."""
        client = CodexAppServerClient()

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value={"success": True}
        ) as mock_send:
            result = await client.login_with_api_key("sk-test-key")

            mock_send.assert_called_once_with(
                "account/login/start",
                {"type": "apiKey", "apiKey": "sk-test-key"},
            )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_get_account_status(self):
        """get_account_status sends request."""
        client = CodexAppServerClient()

        mock_status = {"authenticated": True, "user": "test@example.com"}

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value=mock_status
        ) as mock_send:
            result = await client.get_account_status()

            mock_send.assert_called_once_with("account/status", {})

        assert result["authenticated"] is True


# =============================================================================
# CodexAdapter Tests
# =============================================================================


class TestCodexAdapterInit:
    """Tests for CodexAdapter initialization."""

    def test_default_init(self) -> None:
        """Default initialization."""
        adapter = CodexAdapter()

        assert adapter._hook_manager is None
        assert adapter._codex_client is None
        assert adapter._machine_id is None
        assert adapter._attached is False
        assert adapter.source == SessionSource.CODEX

    def test_with_hook_manager(self) -> None:
        """Initialize with hook manager."""
        mock_hook_manager = MagicMock()
        adapter = CodexAdapter(hook_manager=mock_hook_manager)

        assert adapter._hook_manager is mock_hook_manager


class TestCodexAdapterIsAvailable:
    """Tests for is_codex_available static method."""

    @patch("shutil.which")
    def test_codex_available(self, mock_which) -> None:
        """Returns True when codex is in PATH."""
        mock_which.return_value = "/usr/local/bin/codex"

        assert CodexAdapter.is_codex_available() is True
        mock_which.assert_called_once_with("codex")

    @patch("shutil.which")
    def test_codex_not_available(self, mock_which) -> None:
        """Returns False when codex is not in PATH."""
        mock_which.return_value = None

        assert CodexAdapter.is_codex_available() is False


class TestCodexAdapterMachineId:
    """Tests for machine ID handling."""

    def test_get_machine_id_cached(self) -> None:
        """Machine ID is cached after first call."""
        adapter = CodexAdapter()

        id1 = adapter._get_machine_id()
        id2 = adapter._get_machine_id()

        assert id1 == id2
        assert adapter._machine_id == id1


class TestCodexAdapterAttachDetach:
    """Tests for attach/detach from client."""

    def test_attach_to_client(self) -> None:
        """Attaching registers notification handlers."""
        adapter = CodexAdapter()
        mock_client = MagicMock()

        adapter.attach_to_client(mock_client)

        assert adapter._attached is True
        assert adapter._codex_client is mock_client

        # Should register handlers for tracking events
        calls = mock_client.add_notification_handler.call_args_list
        methods_registered = [c[0][0] for c in calls]
        assert "thread/started" in methods_registered
        assert "turn/started" in methods_registered
        assert "turn/completed" in methods_registered
        assert "item/completed" in methods_registered

    def test_attach_when_already_attached(self) -> None:
        """Attaching when already attached is a no-op."""
        adapter = CodexAdapter()
        adapter._attached = True
        mock_client = MagicMock()

        adapter.attach_to_client(mock_client)

        mock_client.add_notification_handler.assert_not_called()
        assert mock_client.add_notification_handler.call_count == 0
        assert not mock_client.add_notification_handler.called

    def test_detach_from_client(self) -> None:
        """Detaching removes notification handlers."""
        adapter = CodexAdapter()
        mock_client = MagicMock()

        adapter.attach_to_client(mock_client)
        adapter.detach_from_client()

        assert adapter._attached is False
        assert adapter._codex_client is None

        calls = mock_client.remove_notification_handler.call_args_list
        assert len(calls) == len(CodexAdapter.SESSION_TRACKING_EVENTS)

    def test_detach_when_not_attached(self) -> None:
        """Detaching when not attached is a no-op."""
        adapter = CodexAdapter()

        adapter.detach_from_client()
        assert adapter._attached is False
        assert adapter._codex_client is None


class TestCodexAdapterTranslateToHookEvent:
    """Tests for translate_to_hook_event method."""

    def test_thread_started(self) -> None:
        """Translate thread/started to SESSION_START."""
        adapter = CodexAdapter()

        native_event = {
            "method": "thread/started",
            "params": {
                "cwd": "/tmp/project",
                "terminal_context": {"tmux_pane": "%2"},
                "thread": {
                    "id": "thr-123",
                    "preview": "Help me with code",
                    "modelProvider": "openai",
                    "createdAt": 1704067200,
                    "path": "/tmp/codex-session.jsonl",
                },
            },
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.SESSION_START
        assert hook_event.session_id == "thr-123"
        assert hook_event.source == SessionSource.CODEX
        assert hook_event.cwd == "/tmp/project"
        assert hook_event.data["preview"] == "Help me with code"
        assert hook_event.data["model_provider"] == "openai"
        assert hook_event.data["transcript_path"] == "/tmp/codex-session.jsonl"
        assert hook_event.data["terminal_context"]["tmux_pane"] == "%2"

    def test_thread_archive(self) -> None:
        """Translate thread/archive to SESSION_END."""
        adapter = CodexAdapter()

        native_event = {
            "method": "thread/archive",
            "params": {"threadId": "thr-456"},
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.SESSION_END
        assert hook_event.session_id == "thr-456"

    def test_thread_closed(self) -> None:
        """Translate thread/closed to SESSION_END."""
        adapter = CodexAdapter()

        native_event = {
            "method": "thread/closed",
            "params": {"threadId": "thr-closed-1"},
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.SESSION_END
        assert hook_event.session_id == "thr-closed-1"

    def test_turn_started(self) -> None:
        """Translate turn/started to BEFORE_AGENT."""
        adapter = CodexAdapter()

        native_event = {
            "method": "turn/started",
            "params": {
                "threadId": "thr-789",
                "turn": {
                    "id": "turn-1",
                    "status": "inProgress",
                },
            },
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.BEFORE_AGENT
        assert hook_event.session_id == "thr-789"
        assert hook_event.data["turn_id"] == "turn-1"
        assert hook_event.data["status"] == "inProgress"

    def test_turn_started_preserves_prompt_when_available(self) -> None:
        """Translate turn/started with prompt text for broadcaster compatibility."""
        adapter = CodexAdapter()

        native_event = {
            "method": "turn/started",
            "params": {
                "threadId": "thr-789",
                "prompt": "list_mcp_servers",
                "turn": {
                    "id": "turn-1",
                    "status": "inProgress",
                },
            },
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is not None
        assert hook_event.data["prompt"] == "list_mcp_servers"

    def test_turn_completed(self) -> None:
        """Translate turn/completed to AFTER_AGENT."""
        adapter = CodexAdapter()

        native_event = {
            "method": "turn/completed",
            "params": {
                "threadId": "thr-abc",
                "turn": {
                    "id": "turn-2",
                    "status": "completed",
                    "error": None,
                },
            },
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.AFTER_AGENT
        assert hook_event.session_id == "thr-abc"
        assert hook_event.data["status"] == "completed"

    def test_item_completed_tool(self) -> None:
        """Translate item/completed for tool items to AFTER_TOOL."""
        adapter = CodexAdapter()

        for item_type in ["commandExecution", "fileChange", "mcpToolCall"]:
            native_event = {
                "method": "item/completed",
                "params": {
                    "threadId": "thr-tool",
                    "item": {
                        "id": "item-1",
                        "type": item_type,
                        "status": "completed",
                    },
                },
            }

            hook_event = adapter.translate_to_hook_event(native_event)

            assert hook_event is not None
            assert hook_event.event_type == HookEventType.AFTER_TOOL
            assert hook_event.data["item_type"] == item_type

    def test_item_completed_file_change_uses_cached_cwd(self, tmp_path: Path) -> None:
        """Cached thread cwd should flow into synthetic AFTER_TOOL hook events."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        in_repo_file = repo_root / "src" / "main.py"
        in_repo_file.parent.mkdir(parents=True)

        client = CodexAppServerClient()
        client._thread_cwds["thr-tool"] = str(repo_root)
        params = client._enrich_notification(
            "item/completed",
            {
                "threadId": "thr-tool",
                "item": {
                    "id": "item-file-1",
                    "type": "fileChange",
                    "status": "completed",
                    "fileChange": {
                        "input": [{"path": str(in_repo_file), "content": "print('ok')"}]
                    },
                },
            },
        )

        adapter = CodexAdapter()
        hook_event = adapter.translate_to_hook_event({"method": "item/completed", "params": params})

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.AFTER_TOOL
        assert hook_event.cwd == str(repo_root)
        assert hook_event.data["tool_name"] == "Write"
        assert hook_event.data["tool_input"]["file_path"] == str(in_repo_file)

    def test_item_completed_mcp_tool_uses_raw_completed_fields(self) -> None:
        """Tool-shaped item/completed payloads should preserve MCP tool identity."""
        adapter = CodexAdapter()

        native_event = {
            "method": "item/completed",
            "params": {
                "threadId": "thr-tool",
                "item": {
                    "id": "item-mcp-1",
                    "status": "completed",
                    "name": "mcp__gobby__get_tool_schema",
                    "arguments": json.dumps(
                        {"server_name": "gobby-tasks", "tool_name": "claim_task"}
                    ),
                    "output": {"success": True},
                },
            },
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.AFTER_TOOL
        assert hook_event.data["tool_name"] == "mcp__gobby__get_tool_schema"
        assert hook_event.data["tool_input"] == {
            "server_name": "gobby-tasks",
            "tool_name": "claim_task",
        }
        assert hook_event.data["mcp_server"] == "gobby"
        assert hook_event.data["mcp_tool"] == "get_tool_schema"
        assert hook_event.data["tool_output"] == {"success": True}

    def test_item_completed_file_change_out_of_repo_does_not_mark_had_edits(
        self, tmp_path: Path
    ) -> None:
        """Synthetic Codex AFTER_TOOL edits outside cwd should not mark had_edits."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        outside_file = tmp_path / "outside" / "settings.json"
        outside_file.parent.mkdir(parents=True)

        client = CodexAppServerClient()
        client._thread_cwds["thr-tool"] = str(repo_root)
        params = client._enrich_notification(
            "item/completed",
            {
                "threadId": "thr-tool",
                "item": {
                    "id": "item-file-2",
                    "type": "fileChange",
                    "status": "completed",
                    "fileChange": {
                        "input": [{"path": str(outside_file), "content": '{"ok":true}'}]
                    },
                },
            },
        )

        adapter = CodexAdapter()
        hook_event = adapter.translate_to_hook_event({"method": "item/completed", "params": params})
        assert hook_event is not None
        hook_event.metadata["_platform_session_id"] = "sess-123"

        session_storage = MagicMock()
        task_manager = MagicMock()
        task_manager.list_tasks.return_value = [MagicMock()]
        handlers = EventHandlers(session_storage=session_storage, task_manager=task_manager)

        handlers.handle_after_tool(hook_event)

        session_storage.mark_had_edits.assert_not_called()

    def test_item_completed_file_change_in_repo_marks_had_edits(self, tmp_path: Path) -> None:
        """Synthetic Codex AFTER_TOOL edits inside cwd should still mark had_edits."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        in_repo_file = repo_root / "src" / "main.py"
        in_repo_file.parent.mkdir(parents=True)

        client = CodexAppServerClient()
        client._thread_cwds["thr-tool"] = str(repo_root)
        params = client._enrich_notification(
            "item/completed",
            {
                "threadId": "thr-tool",
                "item": {
                    "id": "item-file-3",
                    "type": "fileChange",
                    "status": "completed",
                    "fileChange": {
                        "input": [{"path": str(in_repo_file), "content": "print('ok')"}]
                    },
                },
            },
        )

        adapter = CodexAdapter()
        hook_event = adapter.translate_to_hook_event({"method": "item/completed", "params": params})
        assert hook_event is not None
        hook_event.metadata["_platform_session_id"] = "sess-123"

        session_storage = MagicMock()
        task_manager = MagicMock()
        task_manager.list_tasks.return_value = [MagicMock()]
        handlers = EventHandlers(session_storage=session_storage, task_manager=task_manager)

        handlers.handle_after_tool(hook_event)

        session_storage.mark_had_edits.assert_called_once_with("sess-123")

    def test_item_completed_mcp_tool_derives_name_from_server_and_tool(self) -> None:
        """mcpToolCall completions without name should still normalize tool identity."""
        adapter = CodexAdapter()

        native_event = {
            "method": "item/completed",
            "params": {
                "threadId": "thr-tool",
                "item": {
                    "id": "item-mcp-2",
                    "type": "mcpToolCall",
                    "server": "gobby",
                    "tool": "list_mcp_servers",
                    "status": "completed",
                    "arguments": {},
                    "result": {"content": []},
                },
            },
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is not None
        assert hook_event.data["tool_name"] == "mcp__gobby__list_mcp_servers"
        assert hook_event.data["mcp_server"] == "gobby"
        assert hook_event.data["mcp_tool"] == "list_mcp_servers"

    def test_item_completed_non_tool(self) -> None:
        """item/completed for non-tool items returns None."""
        adapter = CodexAdapter()

        native_event = {
            "method": "item/completed",
            "params": {
                "threadId": "thr-msg",
                "item": {
                    "id": "item-2",
                    "type": "agentMessage",  # Not a tool type
                    "status": "completed",
                },
            },
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is None

    def test_item_completed_context_compaction(self) -> None:
        """item/completed with contextCompaction routes to PRE_COMPACT."""
        adapter = CodexAdapter()

        native_event = {
            "method": "item/completed",
            "params": {
                "threadId": "thr-compact",
                "item": {
                    "id": "item-compact-1",
                    "type": "contextCompaction",
                    "status": "completed",
                },
            },
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.PRE_COMPACT
        assert hook_event.session_id == "thr-compact"
        assert hook_event.data["trigger"] == "auto"
        assert hook_event.data["item_id"] == "item-compact-1"
        assert hook_event.data["item_type"] == "contextCompaction"

    def test_unknown_event(self) -> None:
        """Unknown event types return None."""
        adapter = CodexAdapter()

        native_event = {
            "method": "unknown/event",
            "params": {},
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is None


class TestCodexAdapterTranslateApprovalEvent:
    """Tests for _translate_approval_event method."""

    def test_command_execution_approval(self) -> None:
        """Translate command execution approval request."""
        adapter = CodexAdapter()

        hook_event = adapter._translate_approval_event(
            "item/commandExecution/requestApproval",
            {
                "threadId": "thr-cmd",
                "itemId": "item-cmd",
                "turnId": "turn-1",
                "parsedCmd": "rm -rf /",
                "reason": "destructive operation",
                "risk": "high",
            },
        )

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.BEFORE_TOOL
        assert hook_event.session_id == "thr-cmd"
        assert hook_event.data["tool_name"] == "Bash"
        assert hook_event.data["tool_input"] == "rm -rf /"
        assert hook_event.metadata["requires_response"] is True

    def test_file_change_approval(self) -> None:
        """Translate file change approval request."""
        adapter = CodexAdapter()

        changes = [{"path": "/file.txt", "content": "new content"}]
        hook_event = adapter._translate_approval_event(
            "item/fileChange/requestApproval",
            {
                "threadId": "thr-file",
                "itemId": "item-file",
                "changes": changes,
            },
        )

        assert hook_event is not None
        assert hook_event.data["tool_name"] == "Write"
        assert hook_event.data["tool_input"] == {
            "changes": changes,
            "file_path": "/file.txt",
        }

    def test_mcp_tool_call_approval(self) -> None:
        """Translate MCP approval requests to BEFORE_TOOL with raw tool identity."""
        adapter = CodexAdapter()

        hook_event = adapter._translate_approval_event(
            "item/mcpToolCall/requestApproval",
            {
                "threadId": "thr-mcp",
                "itemId": "item-mcp",
                "turnId": "turn-1",
                "reason": "task lookup",
                "risk": "low",
                "mcpToolCall": {
                    "name": "mcp__gobby__get_tool_schema",
                    "arguments": json.dumps(
                        {"server_name": "gobby-tasks", "tool_name": "claim_task"}
                    ),
                },
            },
        )

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.BEFORE_TOOL
        assert hook_event.session_id == "thr-mcp"
        assert hook_event.data["tool_name"] == "mcp__gobby__get_tool_schema"
        assert hook_event.data["tool_input"] == {
            "server_name": "gobby-tasks",
            "tool_name": "claim_task",
        }
        assert hook_event.data["mcp_server"] == "gobby"
        assert hook_event.data["mcp_tool"] == "get_tool_schema"
        assert hook_event.metadata["approval_method"] == "item/mcpToolCall/requestApproval"
        assert hook_event.metadata["original_tool_name"] == "mcp__gobby__get_tool_schema"
        assert hook_event.metadata["normalized_tool_name"] == "mcp__gobby__get_tool_schema"

    def test_mcp_elicitation_request_approval(self) -> None:
        """Translate MCP elicitation approvals into BEFORE_TOOL hook events."""
        adapter = CodexAdapter()

        hook_event = adapter._translate_approval_event(
            "mcpServer/elicitation/request",
            {
                "threadId": "thr-mcp",
                "turnId": "turn-1",
                "serverName": "gobby",
                "mode": "form",
                "message": 'Allow the gobby MCP server to run tool "list_mcp_servers"?',
                "requestedSchema": {"type": "object", "properties": {}},
                "_meta": {
                    "codex_approval_kind": "mcp_tool_call",
                    "tool_params": {},
                },
            },
        )

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.BEFORE_TOOL
        assert hook_event.session_id == "thr-mcp"
        assert hook_event.data["tool_name"] == "mcp__gobby__list_mcp_servers"
        assert hook_event.data["tool_input"] == {}
        assert hook_event.data["mcp_server"] == "gobby"
        assert hook_event.data["mcp_tool"] == "list_mcp_servers"
        assert hook_event.metadata["approval_method"] == "mcpServer/elicitation/request"

    def test_unknown_approval_method(self) -> None:
        """Unknown approval method returns None."""
        adapter = CodexAdapter()

        hook_event = adapter._translate_approval_event(
            "unknown/requestApproval",
            {"threadId": "thr-1"},
        )

        assert hook_event is None


class TestCodexAdapterTranslateFromHookResponse:
    """Tests for translate_from_hook_response method."""

    def test_allow_response(self) -> None:
        """Allow response maps to accept."""
        adapter = CodexAdapter()

        response = HookResponse(decision="allow")
        result = adapter.translate_from_hook_response(response)

        assert result["decision"] == "accept"

    def test_deny_response(self) -> None:
        """Deny response maps to decline."""
        adapter = CodexAdapter()

        response = HookResponse(decision="deny")
        result = adapter.translate_from_hook_response(response)

        assert result["decision"] == "decline"

    def test_block_response(self) -> None:
        """Block response maps to cancel."""
        adapter = CodexAdapter()

        response = HookResponse(decision="block")
        result = adapter.translate_from_hook_response(response)

        assert result["decision"] == "cancel"

    def test_auto_approve_response(self) -> None:
        """Auto-approve maps to acceptForSession."""
        adapter = CodexAdapter()

        response = HookResponse(decision="allow", auto_approve=True)
        result = adapter.translate_from_hook_response(response)

        assert result["decision"] == "acceptForSession"

    def test_exec_policy_amendment_response(self) -> None:
        """Exec policy amendment maps to acceptWithExecpolicyAmendment."""
        adapter = CodexAdapter()

        amendment = {"allow": ["npm test"]}
        response = HookResponse(
            decision="allow",
            metadata={"exec_policy_amendment": amendment},
        )
        result = adapter.translate_from_hook_response(response)

        assert result["decision"] == "acceptWithExecpolicyAmendment"
        assert result["execPolicyAmendment"] == amendment

    def test_other_decisions_map_to_accept(self) -> None:
        """Non-deny/block decisions map to accept."""
        adapter = CodexAdapter()

        for decision in ["allow", "ask", "modify"]:
            response = HookResponse(decision=decision)
            result = adapter.translate_from_hook_response(response)
            assert result["decision"] == "accept"


class TestCodexAdapterParseTimestamp:
    """Tests for _parse_timestamp method."""

    def test_valid_timestamp(self) -> None:
        """Parse valid Unix timestamp."""
        adapter = CodexAdapter()

        # 1704067200 = 2024-01-01 00:00:00 UTC
        # Note: _parse_timestamp returns local time, not UTC
        dt = adapter._parse_timestamp(1704067200)

        # Just verify it parsed successfully and returns a datetime
        # The exact year depends on timezone
        assert dt is not None
        assert hasattr(dt, "year")
        # The timestamp should be in late Dec 2023 or early Jan 2024 depending on timezone
        assert dt.year in (2023, 2024)

    def test_none_timestamp(self) -> None:
        """None timestamp returns now."""
        adapter = CodexAdapter()

        dt = adapter._parse_timestamp(None)

        # Should be close to now
        assert (datetime.now(UTC) - dt).total_seconds() < 5

    def test_invalid_timestamp(self) -> None:
        """Invalid timestamp returns now."""
        adapter = CodexAdapter()

        dt = adapter._parse_timestamp(-999999999999999)  # Invalid

        assert (datetime.now(UTC) - dt).total_seconds() < 5


class TestCodexAdapterHandleNotification:
    """Tests for _handle_notification callback."""

    def test_handle_notification_processes_event(self) -> None:
        """Notification is processed through hook manager."""
        mock_hook_manager = MagicMock()
        adapter = CodexAdapter(hook_manager=mock_hook_manager)

        adapter._handle_notification(
            "turn/started",
            {"threadId": "thr-1", "turn": {"id": "turn-1", "status": "inProgress"}},
        )

        mock_hook_manager.handle.assert_called_once()
        call_args = mock_hook_manager.handle.call_args[0]
        assert call_args[0].event_type == HookEventType.BEFORE_AGENT

    def test_handle_notification_without_hook_manager(self) -> None:
        """Notification without hook manager is silently ignored."""
        adapter = CodexAdapter()

        adapter._handle_notification("turn/started", {"threadId": "thr-1"})
        assert adapter._hook_manager is None

    def test_handle_notification_error_handling(self) -> None:
        """Errors in notification handling are caught."""
        mock_hook_manager = MagicMock()
        mock_hook_manager.handle.side_effect = Exception("Processing error")
        adapter = CodexAdapter(hook_manager=mock_hook_manager)

        adapter._handle_notification("turn/started", {"threadId": "thr-1"})
        assert mock_hook_manager.handle.call_count == 1


class TestCodexAdapterDispatchHookEvent:
    """Tests for CodexAdapter hook dispatch fallback behavior."""

    @staticmethod
    def _event() -> HookEvent:
        return HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id="thr-dispatch",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={},
        )

    @pytest.mark.asyncio
    async def test_dispatch_ignores_non_coroutine_handle_async(self) -> None:
        """Non-coroutine handle_async is ignored in favor of the sync handler."""
        mock_hook_manager = MagicMock()
        mock_hook_manager.handle.return_value = HookResponse(decision="allow")
        mock_hook_manager.handle_async = MagicMock(return_value=HookResponse(decision="deny"))
        adapter = CodexAdapter(hook_manager=mock_hook_manager)

        response = await adapter._dispatch_hook_event(self._event())

        assert response.decision == "allow"
        mock_hook_manager.handle.assert_called_once()
        mock_hook_manager.handle_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_rejects_non_coroutine_handle_async_without_handle(self) -> None:
        """Non-coroutine handle_async alone is not treated as a valid handler."""

        class NonAsyncOnlyHookManager:
            def handle_async(self, _event: HookEvent) -> HookResponse:
                return HookResponse(decision="allow")

        adapter = CodexAdapter(hook_manager=NonAsyncOnlyHookManager())

        with pytest.raises(RuntimeError, match="hook manager has no handle method"):
            await adapter._dispatch_hook_event(self._event())


class TestCodexAdapterSyncExistingSessions:
    """Tests for sync_existing_sessions method."""

    @pytest.mark.asyncio
    async def test_sync_without_hook_manager(self):
        """Sync without hook manager returns 0."""
        adapter = CodexAdapter()

        result = await adapter.sync_existing_sessions()

        assert result == 0

    @pytest.mark.asyncio
    async def test_sync_without_client(self):
        """Sync without client returns 0."""
        adapter = CodexAdapter(hook_manager=MagicMock())

        result = await adapter.sync_existing_sessions()

        assert result == 0

    @pytest.mark.asyncio
    async def test_sync_when_client_not_connected(self):
        """Sync when client not connected returns 0."""
        adapter = CodexAdapter(hook_manager=MagicMock())
        mock_client = MagicMock()
        mock_client.is_connected = False
        adapter._codex_client = mock_client

        result = await adapter.sync_existing_sessions()

        assert result == 0

    @pytest.mark.asyncio
    async def test_sync_existing_sessions_success(self):
        """Sync processes threads through hook manager."""
        mock_hook_manager = MagicMock()
        adapter = CodexAdapter(hook_manager=mock_hook_manager)

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.list_threads = AsyncMock(
            return_value=(
                [
                    CodexThread(id="thr-1", preview="Thread 1", created_at=1000),
                    CodexThread(id="thr-2", preview="Thread 2", created_at=2000),
                ],
                None,  # No next cursor
            )
        )
        adapter._codex_client = mock_client
        adapter._attached = True

        result = await adapter.sync_existing_sessions()

        assert result == 2
        assert mock_hook_manager.handle.call_count == 2

    @pytest.mark.asyncio
    async def test_sync_handles_pagination(self):
        """Sync handles multiple pages of threads."""
        mock_hook_manager = MagicMock()
        adapter = CodexAdapter(hook_manager=mock_hook_manager)

        mock_client = MagicMock()
        mock_client.is_connected = True

        # Return two pages
        page1 = ([CodexThread(id="thr-1")], "cursor-1")
        page2 = ([CodexThread(id="thr-2")], None)

        mock_client.list_threads = AsyncMock(side_effect=[page1, page2])
        adapter._codex_client = mock_client
        adapter._attached = True

        result = await adapter.sync_existing_sessions()

        assert result == 2
        assert mock_client.list_threads.call_count == 2


# =============================================================================
# CodexNotifyAdapter Tests
# =============================================================================


class TestCodexHooksAdapterInit:
    """Tests for CodexHooksAdapter initialization."""

    def test_default_init(self) -> None:
        """Default initialization."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()

        assert adapter._hook_manager is None
        assert adapter.source == SessionSource.CODEX

    def test_with_hook_manager(self) -> None:
        """Initialize with hook manager."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        mock_hook_manager = MagicMock()
        adapter = CodexHooksAdapter(hook_manager=mock_hook_manager)

        assert adapter._hook_manager is mock_hook_manager

    def test_backward_compat_alias(self) -> None:
        """CodexNotifyAdapter is an alias for CodexHooksAdapter."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        assert CodexNotifyAdapter is CodexHooksAdapter


class TestCodexHooksAdapterTranslateToHookEvent:
    """Tests for translate_to_hook_event method."""

    def test_translate_session_start(self) -> None:
        """Translate SessionStart to SESSION_START."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()

        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "codex-session-123",
                "cwd": "/project/path",
                "model": "o3",
            },
            "source": "codex",
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.SESSION_START
        assert hook_event.session_id == "codex-session-123"
        assert hook_event.source == SessionSource.CODEX
        assert hook_event.cwd == "/project/path"

    def test_translate_pre_tool_use(self) -> None:
        """Translate PreToolUse to BEFORE_TOOL."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()

        native_event = {
            "hook_type": "PreToolUse",
            "input_data": {
                "session_id": "codex-session-123",
                "cwd": "/project",
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            },
            "source": "codex",
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.BEFORE_TOOL
        assert hook_event.session_id == "codex-session-123"

    def test_translate_pre_tool_use_apply_patch_as_write(self) -> None:
        """Translate apply_patch to canonical Write with touched file paths."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()

        native_event = {
            "hook_type": "PreToolUse",
            "input_data": {
                "session_id": "codex-session-123",
                "cwd": "/project",
                "tool_name": "apply_patch",
                "tool_input": (
                    "*** Begin Patch\n"
                    "*** Update File: src/main.py\n"
                    "@@\n"
                    "-print('old')\n"
                    "+print('new')\n"
                    "*** End Patch\n"
                ),
            },
            "source": "codex",
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.BEFORE_TOOL
        assert hook_event.data["tool_name"] == "Write"
        assert hook_event.data["tool_input"]["file_path"] == "src/main.py"
        assert hook_event.metadata["original_tool_name"] == "apply_patch"
        assert hook_event.metadata["normalized_tool_name"] == "Write"

    @pytest.mark.parametrize(
        ("tool_name", "expected_tool_name"),
        [
            ("read_file", "Read"),
            ("write_file", "Write"),
            ("edit_file", "Edit"),
            ("run_shell_command", "Bash"),
        ],
    )
    def test_translate_pre_tool_use_normalizes_raw_codex_tool_names(
        self, tool_name: str, expected_tool_name: str
    ) -> None:
        """Translate raw Codex terminal tool names to canonical rule names."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()

        hook_event = adapter.translate_to_hook_event(
            {
                "hook_type": "PreToolUse",
                "input_data": {
                    "session_id": "codex-session-123",
                    "cwd": "/project",
                    "tool_name": tool_name,
                    "tool_input": {"path": "src/app.py"},
                },
                "source": "codex",
            }
        )

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.BEFORE_TOOL
        assert hook_event.data["tool_name"] == expected_tool_name
        if tool_name in {"read_file", "write_file", "edit_file"}:
            assert hook_event.metadata["original_tool_name"] == tool_name
            assert hook_event.metadata["normalized_tool_name"] == expected_tool_name

    def test_translate_post_tool_use(self) -> None:
        """Translate PostToolUse to AFTER_TOOL."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()

        native_event = {
            "hook_type": "PostToolUse",
            "input_data": {
                "session_id": "codex-session-123",
                "cwd": "/project",
                "tool_name": "Bash",
            },
            "source": "codex",
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.AFTER_TOOL

    def test_translate_user_prompt_submit(self) -> None:
        """Translate UserPromptSubmit to BEFORE_AGENT."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()

        native_event = {
            "hook_type": "UserPromptSubmit",
            "input_data": {
                "session_id": "codex-session-123",
                "cwd": "/project",
            },
            "source": "codex",
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.BEFORE_AGENT

    def test_translate_stop(self) -> None:
        """Translate Stop to STOP."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()

        native_event = {
            "hook_type": "Stop",
            "input_data": {
                "session_id": "codex-session-123",
                "cwd": "/project",
            },
            "source": "codex",
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is not None
        assert hook_event.event_type == HookEventType.STOP

    def test_translate_unsupported_returns_none(self) -> None:
        """Unsupported hook type returns None."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()

        native_event = {
            "hook_type": "AgentTurnComplete",
            "input_data": {"session_id": "thread-123"},
            "source": "codex",
        }

        hook_event = adapter.translate_to_hook_event(native_event)

        assert hook_event is None

    @pytest.mark.parametrize(
        ("hook_type", "expected_event_type"),
        [
            ("PermissionRequest", HookEventType.PERMISSION_REQUEST),
            ("PreCompact", HookEventType.PRE_COMPACT),
            ("PostCompact", HookEventType.POST_COMPACT),
        ],
    )
    def test_translate_new_codex_hook_events(
        self, hook_type: str, expected_event_type: HookEventType
    ) -> None:
        """Translate new Codex 0.129 hook events to unified events."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()

        hook_event = adapter.translate_to_hook_event(
            {
                "hook_type": hook_type,
                "input_data": {"session_id": "thread-123"},
                "source": "codex",
            }
        )

        assert hook_event is not None
        assert hook_event.event_type == expected_event_type

    def test_all_event_types_mapped(self) -> None:
        """All 8 Codex hook types are in EVENT_MAP."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        expected = {
            "PreToolUse",
            "PermissionRequest",
            "PostToolUse",
            "PreCompact",
            "PostCompact",
            "SessionStart",
            "UserPromptSubmit",
            "Stop",
        }
        assert set(CodexHooksAdapter.EVENT_MAP.keys()) == expected


class TestCodexHooksAdapterTranslateFromHookResponse:
    """Tests for translate_from_hook_response method."""

    def test_allow_response_minimal(self) -> None:
        """Allow response includes continue: true."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(decision="allow")
        result = adapter.translate_from_hook_response(response)

        assert result["continue"] is True
        assert "suppressOutput" not in result
        assert "decision" not in result

    def test_block_response(self) -> None:
        """Block response has no suppressOutput so block reason is visible."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(decision="block", reason="Blocked by rule")
        result = adapter.translate_from_hook_response(response)

        assert result["continue"] is False
        assert result["decision"] == "block"
        assert result["reason"] == "Blocked by rule"

    def test_deny_response(self) -> None:
        """Deny response maps to block with continue: false."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(decision="deny", reason="Not allowed")
        result = adapter.translate_from_hook_response(response)

        assert result["continue"] is False
        assert result["decision"] == "block"
        assert result["reason"] == "Not allowed"

    def test_permission_request_allow_uses_decision_behavior(self) -> None:
        """PermissionRequest allow must use Codex decision.behavior."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(decision="allow", modified_input={"command": "echo rewritten"})
        result = adapter.translate_from_hook_response(response, hook_type="PermissionRequest")

        assert result["continue"] is True
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "PermissionRequest"
        assert hso["decision"] == {"behavior": "allow"}
        assert "updatedInput" not in result
        assert "updatedPermissions" not in result
        assert "interrupt" not in result
        assert "updatedInput" not in hso
        assert "updatedPermissions" not in hso
        assert "interrupt" not in hso

    def test_permission_request_deny_uses_decision_behavior(self) -> None:
        """PermissionRequest deny must not use top-level block fields."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(decision="deny", reason="Not allowed")
        result = adapter.translate_from_hook_response(response, hook_type="PermissionRequest")

        assert result["continue"] is True
        assert "decision" not in result
        assert "reason" not in result
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "PermissionRequest"
        assert hso["decision"] == {"behavior": "deny", "message": "Not allowed"}
        assert "updatedInput" not in result
        assert "updatedPermissions" not in result
        assert "interrupt" not in result
        assert "updatedInput" not in hso
        assert "updatedPermissions" not in hso
        assert "interrupt" not in hso

    def test_permission_request_deny_preserves_context_and_metadata(self) -> None:
        """PermissionRequest deny still uses shared context assembly."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(
            decision="deny",
            reason="Not allowed",
            system_message="Session banner",
            context="Rule context",
            metadata={
                "session_id": "session-uuid",
                "session_ref": "#123",
                "_first_hook_for_session": True,
            },
        )
        result = adapter.translate_from_hook_response(response, hook_type="PermissionRequest")

        hso = result["hookSpecificOutput"]
        assert hso["decision"] == {"behavior": "deny", "message": "Not allowed"}
        assert "Session banner" in result["systemMessage"]
        assert "Rule context" in result["systemMessage"]
        assert "Gobby Session ID: #123 (session-uuid)" in result["systemMessage"]

    @pytest.mark.parametrize("hook_type", ["PreCompact", "PostCompact"])
    def test_compact_block_uses_continue_false_stop_reason(self, hook_type: str) -> None:
        """Compact blocks use only universal output fields."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(decision="block", reason="Compaction blocked")
        result = adapter.translate_from_hook_response(response, hook_type=hook_type)

        assert result == {"continue": False, "stopReason": "Compaction blocked"}
        assert "decision" not in result
        assert "reason" not in result
        assert "hookSpecificOutput" not in result

    def test_pre_tool_use_block_uses_permission_decision(self) -> None:
        """PreToolUse blocks must use Codex permissionDecision, not continue=false."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(
            decision="block",
            reason="Tool not allowed",
            system_message="Use MCP instead",
            context="Run create_task first",
            modified_input={"command": "echo rewritten"},
        )
        result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

        assert "continue" not in result
        assert result["decision"] == "block"
        assert result["reason"] == "Tool not allowed"
        assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "Tool not allowed"
        assert "updatedInput" not in result
        assert "updatedInput" not in result["hookSpecificOutput"]
        assert "Use MCP instead" in result["systemMessage"]
        assert "Run create_task first" in result["systemMessage"]

    def test_pre_tool_use_rewrite_does_not_surface_retry_input(self) -> None:
        """PreToolUse rewrites use native updatedInput without retry instructions."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        rewritten = {"command": "uv run python hello.py"}
        response = HookResponse(
            decision="allow",
            context="Bare python is not allowed",
            modified_input=rewritten,
            auto_approve=True,
        )
        result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

        assert result["continue"] is True
        assert "decision" not in result
        assert "updatedInput" not in result
        assert "updatedPermissions" not in result
        assert "interrupt" not in result
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "allow"
        assert hso["updatedInput"] == rewritten
        assert "Bare python is not allowed" in result["systemMessage"]
        assert "uv run python hello.py" not in result["systemMessage"]

    def test_pre_tool_use_wrapper_only_call_tool_rewrite_does_not_emit_retry_blob(self) -> None:
        """Wrapper-only call_tool reshapes should auto-heal without visible retry JSON."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(
            decision="allow",
            modified_input={
                "server_name": "gobby-skills",
                "tool_name": "get_skill",
                "arguments": {"name": "brevity"},
            },
            auto_approve=True,
            metadata={
                "_normalized_tool_name": "mcp__gobby__call_tool",
                "_raw_tool_input": {
                    "arguments": {
                        "server_name": "gobby-skills",
                        "tool_name": "get_skill",
                        "name": "brevity",
                    }
                },
            },
        )

        result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

        assert result["continue"] is True
        assert "decision" not in result
        assert "systemMessage" not in result
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "allow"
        assert hso["updatedInput"] == response.modified_input

    def test_pre_tool_use_modified_input_emits_native_updated_input(self) -> None:
        """modified_input alone becomes native Codex updatedInput."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(
            decision="allow",
            modified_input={"command": "sed -n '1,20p' file.txt"},
        )
        result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

        assert result["continue"] is True
        assert "decision" not in result
        assert result["hookSpecificOutput"] == {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {"command": "sed -n '1,20p' file.txt"},
        }

    def test_context_injection_session_start(self) -> None:
        """SessionStart uses hookSpecificOutput.additionalContext."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(decision="allow", context="Rule injected context")
        result = adapter.translate_from_hook_response(response, hook_type="SessionStart")

        assert result["continue"] is True
        assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "Rule injected context" in result["hookSpecificOutput"]["additionalContext"]

    def test_context_injection_pre_tool_use_uses_system_message(self) -> None:
        """PreToolUse puts context in systemMessage, not additionalContext."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(decision="allow", context="Rule injected context")
        result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

        assert result["continue"] is True
        assert "hookSpecificOutput" not in result
        assert "Rule injected context" in result["systemMessage"]

    def test_stop_routes_context_to_system_message(self) -> None:
        """Stop routes context to systemMessage (only accepted field for Stop)."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(decision="allow", context="Stop context")
        result = adapter.translate_from_hook_response(response, hook_type="Stop")

        assert result["continue"] is True
        assert "hookSpecificOutput" not in result
        assert "Stop context" in result["systemMessage"]

    def test_stop_combines_system_message_and_context(self) -> None:
        """Stop combines system_message and context in systemMessage without overwrite."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(
            decision="allow",
            system_message="System note",
            context="Rule context",
        )
        result = adapter.translate_from_hook_response(response, hook_type="Stop")

        assert "System note" in result["systemMessage"]
        assert "Rule context" in result["systemMessage"]

    def test_system_message_routes_to_additional_context_for_user_prompt(self) -> None:
        """UserPromptSubmit routes system_message to additionalContext, not systemMessage."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(decision="allow", system_message="Session info")
        result = adapter.translate_from_hook_response(response, hook_type="UserPromptSubmit")

        assert "systemMessage" not in result
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "UserPromptSubmit"
        assert "Session info" in hso["additionalContext"]

    def test_system_message_routes_to_additional_context_for_post_tool_use(self) -> None:
        """PostToolUse routes system_message to additionalContext, not systemMessage."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(decision="allow", system_message="Tool note")
        result = adapter.translate_from_hook_response(response, hook_type="PostToolUse")

        assert "systemMessage" not in result
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "PostToolUse"
        assert "Tool note" in hso["additionalContext"]

    @pytest.mark.parametrize("hook_type", ["PermissionRequest", "PreCompact", "PostCompact"])
    def test_context_without_additional_context_schema_routes_to_system_message(
        self, hook_type: str
    ) -> None:
        """Events without additionalContext schemas route context to systemMessage."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(decision="allow", context="Rule context")
        result = adapter.translate_from_hook_response(response, hook_type=hook_type)

        if hook_type == "PermissionRequest":
            assert result["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
        else:
            assert "hookSpecificOutput" not in result
        assert "Rule context" in result["systemMessage"]

    def test_system_message_routes_only_to_additional_context_for_session_start(self) -> None:
        """SessionStart keeps the banner in startup context only once."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(decision="allow", system_message="Session banner")
        result = adapter.translate_from_hook_response(response, hook_type="SessionStart")

        assert "systemMessage" not in result
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "SessionStart"
        assert hso["additionalContext"].count("Session banner") == 1

    def test_pre_tool_use_combines_system_message_and_context(self) -> None:
        """PreToolUse combines system_message and context_parts in systemMessage."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(
            decision="allow",
            system_message="Gate note",
            context="Rule constraint",
            modified_input={"command": "uv run python check.py"},
        )
        result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

        assert result["hookSpecificOutput"]["updatedInput"] == {"command": "uv run python check.py"}
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "Gate note" in result["systemMessage"]
        assert "Rule constraint" in result["systemMessage"]

    def test_session_metadata_first_hook(self) -> None:
        """First hook includes full session metadata in additionalContext."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(
            decision="allow",
            metadata={
                "session_id": "abc-123",
                "session_ref": "#100",
                "external_id": "codex-ext-id",
                "_first_hook_for_session": True,
                "project_id": "proj-1",
            },
        )
        result = adapter.translate_from_hook_response(response, hook_type="SessionStart")

        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "SessionStart"
        ctx = hso["additionalContext"]
        assert "Gobby Session ID: #100 (abc-123)" in ctx
        assert "codex-ext-id" in ctx
        assert "proj-1" in ctx

    def test_session_start_banner_and_metadata_include_session_id_once(self) -> None:
        """SessionStart does not duplicate the session ID between banner and metadata."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        banner = "Gobby Session ID: #100 (abc-123)"
        response = HookResponse(
            decision="allow",
            system_message=banner,
            metadata={
                "session_id": "abc-123",
                "session_ref": "#100",
                "external_id": "codex-ext-id",
                "_first_hook_for_session": True,
                "project_id": "proj-1",
            },
        )

        result = adapter.translate_from_hook_response(response, hook_type="SessionStart")

        assert "systemMessage" not in result
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert ctx.count(banner) == 1
        assert "codex-ext-id" in ctx
        assert "proj-1" in ctx

    def test_session_metadata_subsequent_hook(self) -> None:
        """Subsequent hooks do not inject session ref."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(
            decision="allow",
            metadata={
                "session_id": "abc-123",
                "session_ref": "#100",
                "_first_hook_for_session": False,
            },
        )
        result = adapter.translate_from_hook_response(response, hook_type="PostToolUse")

        assert "hookSpecificOutput" not in result

    def test_additional_context_trims_oversized_response_context_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Oversized low-priority context is bounded before the warning safety net."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(
            decision="allow",
            system_message="Session-critical note",
            context="x" * (ADDITIONAL_CONTEXT_LIMIT + 3_000),
        )

        with caplog.at_level(logging.WARNING, logger="gobby.adapters.codex_impl.hooks_adapter"):
            result = adapter.translate_from_hook_response(response, hook_type="UserPromptSubmit")

        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert len(ctx) == ADDITIONAL_CONTEXT_LIMIT
        assert ctx.startswith("Session-critical note\n\n")
        assert ctx.endswith("\n... [truncated]")
        assert "additionalContext truncated" not in caplog.text

    def test_session_metadata_precedes_oversized_response_context(self) -> None:
        """Session metadata remains visible when response.context is over budget."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        response = HookResponse(
            decision="allow",
            context="x" * (ADDITIONAL_CONTEXT_LIMIT + 3_000),
            metadata={
                "session_id": "abc-123",
                "session_ref": "#100",
                "external_id": "codex-ext-id",
                "_first_hook_for_session": True,
                "project_id": "proj-1",
            },
        )

        result = adapter.translate_from_hook_response(response, hook_type="SessionStart")

        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert len(ctx) == ADDITIONAL_CONTEXT_LIMIT
        assert "Gobby Session ID: #100 (abc-123)" in ctx
        assert "codex-ext-id" in ctx
        assert ctx.index("Gobby Session ID") < ctx.index("x")
        assert ctx.endswith("\n... [truncated]")


class TestCodexHooksAdapterHandleNative:
    """Tests for handle_native method."""

    def test_handle_native_success(self) -> None:
        """Handle native event through hook manager."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        mock_hook_manager = MagicMock()
        mock_hook_manager.handle.return_value = HookResponse(decision="allow")

        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "codex-session-handle",
                "cwd": "/project",
            },
            "source": "codex",
        }

        result = adapter.handle_native(native_event, mock_hook_manager)

        mock_hook_manager.handle.assert_called_once()
        assert result.get("continue") is True

    def test_handle_native_unsupported_event(self) -> None:
        """Handle unsupported event returns empty dict."""
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        adapter = CodexHooksAdapter()
        mock_hook_manager = MagicMock()

        native_event = {
            "hook_type": "Unknown",
            "input_data": {},
            "source": "codex",
        }

        result = adapter.handle_native(native_event, mock_hook_manager)

        mock_hook_manager.handle.assert_not_called()
        assert result == {}


# =============================================================================
# Integration Tests
# =============================================================================


class TestCodexAdapterEventMapping:
    """Tests verifying event type mapping constants."""

    def test_event_map_contains_all_supported_events(self) -> None:
        """EVENT_MAP contains all events we claim to support."""
        expected_methods = [
            "thread/started",
            "thread/archive",
            "turn/started",
            "turn/completed",
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/mcpToolCall/requestApproval",
            "item/completed",
        ]

        for method in expected_methods:
            assert method in CodexAdapter.EVENT_MAP

    def test_tool_item_types_complete(self) -> None:
        """TOOL_ITEM_TYPES contains all tool-related item types."""
        assert "commandExecution" in CodexAdapter.TOOL_ITEM_TYPES
        assert "fileChange" in CodexAdapter.TOOL_ITEM_TYPES
        assert "mcpToolCall" in CodexAdapter.TOOL_ITEM_TYPES

    def test_session_tracking_events_complete(self) -> None:
        """SESSION_TRACKING_EVENTS contains necessary events."""
        assert "thread/started" in CodexAdapter.SESSION_TRACKING_EVENTS
        assert "turn/started" in CodexAdapter.SESSION_TRACKING_EVENTS
        assert "turn/completed" in CodexAdapter.SESSION_TRACKING_EVENTS
        assert "item/completed" in CodexAdapter.SESSION_TRACKING_EVENTS


# =============================================================================
# Phase 1: Approval Response Loop Tests
#
# Tests for bidirectional hook support via the Codex app-server protocol.
# Codex sends approval requests as JSON-RPC requests (with both id and method),
# which must be detected, routed to a handler, and responded to.
# =============================================================================


class TestCodexClientApprovalHandlerRegistration:
    """Tests for approval handler registration on CodexAppServerClient."""

    def test_no_approval_handler_by_default(self) -> None:
        """No approval handler registered by default."""
        client = CodexAppServerClient()
        assert client._approval_handler is None

    def test_register_approval_handler(self) -> None:
        """Register an approval handler."""
        client = CodexAppServerClient()

        async def handler(method: str, params: dict) -> dict:
            return {"decision": "accept"}

        client.register_approval_handler(handler)
        assert client._approval_handler is handler

    def test_register_replaces_previous_handler(self) -> None:
        """Registering a new handler replaces the previous one."""
        client = CodexAppServerClient()

        async def handler1(method: str, params: dict) -> dict:
            return {"decision": "accept"}

        async def handler2(method: str, params: dict) -> dict:
            return {"decision": "decline"}

        client.register_approval_handler(handler1)
        client.register_approval_handler(handler2)
        assert client._approval_handler is handler2

    def test_register_none_clears_handler(self) -> None:
        """Registering None clears the handler."""
        client = CodexAppServerClient()

        async def handler(method: str, params: dict) -> dict:
            return {"decision": "accept"}

        client.register_approval_handler(handler)
        client.register_approval_handler(None)
        assert client._approval_handler is None


class TestCodexClientApprovalRequestDetection:
    """Tests for approval request detection in reader loop.

    Codex sends approval requests as JSON-RPC requests (both id AND method).
    The reader loop must detect these as incoming requests (not responses to
    our outgoing requests) and route them to the registered approval handler.
    """

    @pytest.mark.asyncio
    async def test_detects_command_execution_approval(self) -> None:
        """Reader detects commandExecution approval and calls handler."""
        client = CodexAppServerClient()
        received: dict = {}

        async def handler(method: str, params: dict) -> dict:
            received["method"] = method
            received["params"] = params
            return {"decision": "accept"}

        client.register_approval_handler(handler)

        approval_msg = {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thr-test",
                "itemId": "item-1",
                "parsedCmd": "ls -la",
                "reason": "tool use",
            },
        }

        mock_process = MagicMock()
        lines = [json.dumps(approval_msg) + "\n"]
        read_idx = 0

        def mock_readline():
            nonlocal read_idx
            if read_idx < len(lines):
                line = lines[read_idx]
                read_idx += 1
                return line
            return ""

        mock_process.stdout.readline = mock_readline
        mock_process.poll.return_value = 0
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.flush = MagicMock()

        client._process = mock_process
        client._state = CodexConnectionState.CONNECTED

        reader_task = asyncio.create_task(client._read_loop())
        await asyncio.wait_for(reader_task, timeout=2.0)

        assert received["method"] == "item/commandExecution/requestApproval"
        assert received["params"]["threadId"] == "thr-test"
        assert received["params"]["parsedCmd"] == "ls -la"

    @pytest.mark.asyncio
    async def test_detects_file_change_approval(self) -> None:
        """Reader detects fileChange approval request."""
        client = CodexAppServerClient()
        received: dict = {}

        async def handler(method: str, params: dict) -> dict:
            received["method"] = method
            received["params"] = params
            return {"decision": "accept"}

        client.register_approval_handler(handler)

        approval_msg = {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "item/fileChange/requestApproval",
            "params": {
                "threadId": "thr-file",
                "changes": [{"path": "/test.txt", "content": "hello"}],
            },
        }

        mock_process = MagicMock()
        lines = [json.dumps(approval_msg) + "\n"]
        read_idx = 0

        def mock_readline():
            nonlocal read_idx
            if read_idx < len(lines):
                line = lines[read_idx]
                read_idx += 1
                return line
            return ""

        mock_process.stdout.readline = mock_readline
        mock_process.poll.return_value = 0
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.flush = MagicMock()

        client._process = mock_process
        client._state = CodexConnectionState.CONNECTED

        reader_task = asyncio.create_task(client._read_loop())
        await asyncio.wait_for(reader_task, timeout=2.0)

        assert received["method"] == "item/fileChange/requestApproval"
        assert received["params"]["changes"][0]["path"] == "/test.txt"

    @pytest.mark.asyncio
    async def test_detects_mcp_tool_call_approval(self) -> None:
        """Reader detects mcpToolCall approval requests."""
        client = CodexAppServerClient()
        received: dict = {}

        async def handler(method: str, params: dict) -> dict:
            received["method"] = method
            received["params"] = params
            return {"decision": "accept"}

        client.register_approval_handler(handler)

        approval_msg = {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "item/mcpToolCall/requestApproval",
            "params": {
                "threadId": "thr-mcp",
                "itemId": "item-1",
                "name": "mcp__gobby__get_tool_schema",
                "arguments": {"server_name": "gobby-tasks", "tool_name": "claim_task"},
            },
        }

        mock_process = MagicMock()
        lines = [json.dumps(approval_msg) + "\n"]
        read_idx = 0

        def mock_readline():
            nonlocal read_idx
            if read_idx < len(lines):
                line = lines[read_idx]
                read_idx += 1
                return line
            return ""

        mock_process.stdout.readline = mock_readline
        mock_process.poll.return_value = 0
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.flush = MagicMock()

        client._process = mock_process
        client._state = CodexConnectionState.CONNECTED

        reader_task = asyncio.create_task(client._read_loop())
        await asyncio.wait_for(reader_task, timeout=2.0)

        assert received["method"] == "item/mcpToolCall/requestApproval"
        assert received["params"]["name"] == "mcp__gobby__get_tool_schema"
        assert received["params"]["arguments"]["tool_name"] == "claim_task"

    @pytest.mark.asyncio
    async def test_detects_mcp_elicitation_request(self) -> None:
        """Reader routes MCP elicitation requests to the approval handler."""
        client = CodexAppServerClient()
        received: dict = {}

        async def handler(method: str, params: dict) -> dict:
            received["method"] = method
            received["params"] = params
            return {"action": "accept", "content": None, "_meta": None}

        client.register_approval_handler(handler)

        approval_msg = {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "mcpServer/elicitation/request",
            "params": {
                "threadId": "thr-mcp",
                "turnId": "turn-1",
                "serverName": "gobby",
                "mode": "form",
                "message": 'Allow the gobby MCP server to run tool "list_mcp_servers"?',
                "requestedSchema": {"type": "object", "properties": {}},
                "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": {}},
            },
        }

        mock_process = MagicMock()
        lines = [json.dumps(approval_msg) + "\n"]
        read_idx = 0

        def mock_readline():
            nonlocal read_idx
            if read_idx < len(lines):
                line = lines[read_idx]
                read_idx += 1
                return line
            return ""

        mock_process.stdout.readline = mock_readline
        mock_process.poll.return_value = 0
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.flush = MagicMock()

        client._process = mock_process
        client._state = CodexConnectionState.CONNECTED

        reader_task = asyncio.create_task(client._read_loop())
        await asyncio.wait_for(reader_task, timeout=2.0)

        assert received["method"] == "mcpServer/elicitation/request"
        assert received["params"]["serverName"] == "gobby"
        assert received["params"]["_meta"]["codex_approval_kind"] == "mcp_tool_call"

    @pytest.mark.asyncio
    async def test_turn_started_notification_is_enriched_with_prompt(self) -> None:
        """turn/started notifications should carry the original prompt when available."""
        client = CodexAppServerClient()
        received: dict = {}

        def handler(method: str, params: dict) -> None:
            received["method"] = method
            received["params"] = params

        client.add_notification_handler("turn/started", handler)
        client._pending_turn_prompts_by_thread["thr-1"] = "hello world"

        notification_msg = {
            "jsonrpc": "2.0",
            "method": "turn/started",
            "params": {
                "threadId": "thr-1",
                "turn": {"id": "turn-1", "status": "inProgress"},
            },
        }

        mock_process = MagicMock()
        lines = [json.dumps(notification_msg) + "\n"]
        read_idx = 0

        def mock_readline():
            nonlocal read_idx
            if read_idx < len(lines):
                line = lines[read_idx]
                read_idx += 1
                return line
            return ""

        mock_process.stdout.readline = mock_readline
        mock_process.poll.return_value = 0
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.flush = MagicMock()

        client._process = mock_process
        client._state = CodexConnectionState.CONNECTED

        reader_task = asyncio.create_task(client._read_loop())
        await asyncio.wait_for(reader_task, timeout=2.0)

        assert received["method"] == "turn/started"
        assert received["params"]["prompt"] == "hello world"

    @pytest.mark.asyncio
    async def test_distinguishes_approval_from_response(self) -> None:
        """Incoming requests (id+method) don't interfere with pending response futures."""
        client = CodexAppServerClient()
        handler_called = False

        async def handler(method: str, params: dict) -> dict:
            nonlocal handler_called
            handler_called = True
            return {"decision": "accept"}

        client.register_approval_handler(handler)

        # Our outgoing request has id=1
        loop = asyncio.get_event_loop()
        pending_future = loop.create_future()
        client._pending_requests[1] = pending_future

        # Two messages: response to our request (id=1) + approval request (id=42)
        response_msg = {"jsonrpc": "2.0", "id": 1, "result": {"key": "value"}}
        approval_msg = {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thr-1", "parsedCmd": "echo hi"},
        }

        mock_process = MagicMock()
        lines = [json.dumps(response_msg) + "\n", json.dumps(approval_msg) + "\n"]
        read_idx = 0

        def mock_readline():
            nonlocal read_idx
            if read_idx < len(lines):
                line = lines[read_idx]
                read_idx += 1
                return line
            return ""

        mock_process.stdout.readline = mock_readline
        mock_process.poll.return_value = 0
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.flush = MagicMock()

        client._process = mock_process
        client._state = CodexConnectionState.CONNECTED

        reader_task = asyncio.create_task(client._read_loop())
        await asyncio.wait_for(reader_task, timeout=2.0)

        # Response resolved our pending future
        assert pending_future.done()
        assert pending_future.result() == {"key": "value"}

        # Approval handler was called separately
        assert handler_called

    @pytest.mark.asyncio
    async def test_no_handler_sends_error_response(self) -> None:
        """Without approval handler, incoming requests get a JSON-RPC error."""
        client = CodexAppServerClient()

        approval_msg = {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thr-1"},
        }

        mock_process = MagicMock()
        lines = [json.dumps(approval_msg) + "\n"]
        read_idx = 0

        def mock_readline():
            nonlocal read_idx
            if read_idx < len(lines):
                line = lines[read_idx]
                read_idx += 1
                return line
            return ""

        mock_process.stdout.readline = mock_readline
        mock_process.poll.return_value = 0
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.flush = MagicMock()

        client._process = mock_process
        client._state = CodexConnectionState.CONNECTED

        reader_task = asyncio.create_task(client._read_loop())
        await asyncio.wait_for(reader_task, timeout=2.0)

        # Error response sent back with -32601 (method not found)
        mock_process.stdin.write.assert_called_once()
        sent = json.loads(mock_process.stdin.write.call_args[0][0])
        assert sent["jsonrpc"] == "2.0"
        assert sent["id"] == 42
        assert sent["error"]["code"] == -32601


class TestCodexClientApprovalResponseRouting:
    """Tests for approval response routing back to Codex.

    After the approval handler returns a decision, the client must send
    a JSON-RPC response back to Codex with the matching request id.
    """

    @pytest.mark.asyncio
    async def test_sends_accept_response(self) -> None:
        """Accept decision sends JSON-RPC response with accept."""
        client = CodexAppServerClient()
        written_lines: list[str] = []

        async def handler(method: str, params: dict) -> dict:
            return {"decision": "accept"}

        client.register_approval_handler(handler)

        approval_msg = {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thr-1", "parsedCmd": "echo test"},
        }

        mock_process = MagicMock()
        lines = [json.dumps(approval_msg) + "\n"]
        read_idx = 0

        def mock_readline():
            nonlocal read_idx
            if read_idx < len(lines):
                line = lines[read_idx]
                read_idx += 1
                return line
            return ""

        mock_process.stdout.readline = mock_readline
        mock_process.poll.return_value = 0
        mock_process.stdin.write = lambda x: written_lines.append(x)
        mock_process.stdin.flush = MagicMock()

        client._process = mock_process
        client._state = CodexConnectionState.CONNECTED

        reader_task = asyncio.create_task(client._read_loop())
        await asyncio.wait_for(reader_task, timeout=2.0)

        assert len(written_lines) >= 1
        response = json.loads(written_lines[0].strip())
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 42
        assert response["result"]["decision"] == "accept"

    @pytest.mark.asyncio
    async def test_sends_decline_response(self) -> None:
        """Decline decision sends JSON-RPC response with decline."""
        client = CodexAppServerClient()
        written_lines: list[str] = []

        async def handler(method: str, params: dict) -> dict:
            return {"decision": "decline"}

        client.register_approval_handler(handler)

        approval_msg = {
            "jsonrpc": "2.0",
            "id": 55,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thr-1", "parsedCmd": "rm -rf /"},
        }

        mock_process = MagicMock()
        lines = [json.dumps(approval_msg) + "\n"]
        read_idx = 0

        def mock_readline():
            nonlocal read_idx
            if read_idx < len(lines):
                line = lines[read_idx]
                read_idx += 1
                return line
            return ""

        mock_process.stdout.readline = mock_readline
        mock_process.poll.return_value = 0
        mock_process.stdin.write = lambda x: written_lines.append(x)
        mock_process.stdin.flush = MagicMock()

        client._process = mock_process
        client._state = CodexConnectionState.CONNECTED

        reader_task = asyncio.create_task(client._read_loop())
        await asyncio.wait_for(reader_task, timeout=2.0)

        response = json.loads(written_lines[0].strip())
        assert response["id"] == 55
        assert response["result"]["decision"] == "decline"

    @pytest.mark.asyncio
    async def test_handler_error_sends_error_response(self) -> None:
        """Handler exception sends JSON-RPC error response."""
        client = CodexAppServerClient()
        written_lines: list[str] = []

        async def handler(method: str, params: dict) -> dict:
            raise RuntimeError("Hook processing failed")

        client.register_approval_handler(handler)

        approval_msg = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thr-1"},
        }

        mock_process = MagicMock()
        lines = [json.dumps(approval_msg) + "\n"]
        read_idx = 0

        def mock_readline():
            nonlocal read_idx
            if read_idx < len(lines):
                line = lines[read_idx]
                read_idx += 1
                return line
            return ""

        mock_process.stdout.readline = mock_readline
        mock_process.poll.return_value = 0
        mock_process.stdin.write = lambda x: written_lines.append(x)
        mock_process.stdin.flush = MagicMock()

        client._process = mock_process
        client._state = CodexConnectionState.CONNECTED

        reader_task = asyncio.create_task(client._read_loop())
        await asyncio.wait_for(reader_task, timeout=2.0)

        assert len(written_lines) >= 1
        response = json.loads(written_lines[0].strip())
        assert response["id"] == 10
        assert "error" in response
        assert response["error"]["code"] == -32603  # Internal error
        assert "Hook processing failed" in response["error"]["message"]

    @pytest.mark.asyncio
    async def test_response_preserves_request_id(self) -> None:
        """Response id matches the incoming request id."""
        client = CodexAppServerClient()
        written_lines: list[str] = []

        async def handler(method: str, params: dict) -> dict:
            return {"decision": "accept"}

        client.register_approval_handler(handler)

        approval_msg = {
            "jsonrpc": "2.0",
            "id": 99999,
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thr-1", "changes": []},
        }

        mock_process = MagicMock()
        lines = [json.dumps(approval_msg) + "\n"]
        read_idx = 0

        def mock_readline():
            nonlocal read_idx
            if read_idx < len(lines):
                line = lines[read_idx]
                read_idx += 1
                return line
            return ""

        mock_process.stdout.readline = mock_readline
        mock_process.poll.return_value = 0
        mock_process.stdin.write = lambda x: written_lines.append(x)
        mock_process.stdin.flush = MagicMock()

        client._process = mock_process
        client._state = CodexConnectionState.CONNECTED

        reader_task = asyncio.create_task(client._read_loop())
        await asyncio.wait_for(reader_task, timeout=2.0)

        response = json.loads(written_lines[0].strip())
        assert response["id"] == 99999


class TestCodexAdapterApprovalHandling:
    """Tests for CodexAdapter.handle_approval_request with HookManager."""

    @pytest.mark.asyncio
    async def test_handle_approval_calls_hook_manager(self) -> None:
        """handle_approval_request translates and processes through HookManager."""
        mock_hm = MagicMock()
        mock_hm.handle.return_value = HookResponse(decision="allow")
        adapter = CodexAdapter(hook_manager=mock_hm)

        result = await adapter.handle_approval_request(
            "item/commandExecution/requestApproval",
            {"threadId": "thr-cmd", "itemId": "item-1", "parsedCmd": "echo hello"},
        )

        mock_hm.handle.assert_called_once()
        hook_event = mock_hm.handle.call_args[0][0]
        assert hook_event.event_type == HookEventType.BEFORE_TOOL
        assert hook_event.data["tool_name"] == "Bash"
        assert result == {"decision": "accept"}

    @pytest.mark.asyncio
    async def test_handle_approval_deny_maps_to_decline(self) -> None:
        """Denied hook response translates to decline."""
        mock_hm = MagicMock()
        mock_hm.handle.return_value = HookResponse(decision="deny")
        adapter = CodexAdapter(hook_manager=mock_hm)

        result = await adapter.handle_approval_request(
            "item/commandExecution/requestApproval",
            {"threadId": "thr-1", "parsedCmd": "rm -rf /"},
        )

        assert result == {"decision": "decline"}

    @pytest.mark.asyncio
    async def test_handle_approval_without_hook_manager(self) -> None:
        """Without hook manager, approval requests fail closed."""
        adapter = CodexAdapter()

        result = await adapter.handle_approval_request(
            "item/commandExecution/requestApproval",
            {"threadId": "thr-1", "parsedCmd": "ls"},
        )

        assert result == {"decision": "decline"}

    @pytest.mark.asyncio
    async def test_handle_approval_unknown_method(self) -> None:
        """Unknown approval methods fail closed."""
        mock_hm = MagicMock()
        adapter = CodexAdapter(hook_manager=mock_hm)

        result = await adapter.handle_approval_request(
            "unknown/requestApproval",
            {"threadId": "thr-1"},
        )

        assert result == {"decision": "decline"}
        mock_hm.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_approval_auto_accepts_safe_mcp_proxy_discovery(self) -> None:
        """Safe MCP proxy discovery should still fire hooks and force accept."""
        mock_hm = MagicMock()
        mock_hm.handle.return_value = HookResponse(decision="allow")
        adapter = CodexAdapter(hook_manager=mock_hm)

        result = await adapter.handle_approval_request(
            "item/mcpToolCall/requestApproval",
            {
                "threadId": "thr-mcp-safe",
                "itemId": "item-mcp-safe",
                "name": "mcp__gobby__get_tool_schema",
                "arguments": {
                    "server_name": "gobby-tasks",
                    "tool_name": "create_task",
                },
            },
        )

        assert result == {"decision": "accept"}
        mock_hm.handle.assert_called_once()
        hook_event = mock_hm.handle.call_args[0][0]
        assert hook_event.event_type == HookEventType.BEFORE_TOOL
        assert hook_event.data["tool_name"] == "mcp__gobby__get_tool_schema"
        assert hook_event.data["tool_input"] == {
            "server_name": "gobby-tasks",
            "tool_name": "create_task",
        }

    @pytest.mark.asyncio
    async def test_handle_approval_safe_mcp_proxy_forces_accept_even_when_hook_denies(self) -> None:
        """Safe MCP proxy discovery should ignore deny/block responses from hooks."""
        mock_hm = MagicMock()
        mock_hm.handle.return_value = HookResponse(
            decision="deny",
            reason="Should not surface for safe discovery tools",
        )
        adapter = CodexAdapter(hook_manager=mock_hm)

        result = await adapter.handle_approval_request(
            "item/mcpToolCall/requestApproval",
            {
                "threadId": "thr-mcp-safe",
                "itemId": "item-mcp-safe",
                "name": "mcp__gobby__get_tool_schema",
                "arguments": {
                    "server_name": "gobby-tasks",
                    "tool_name": "create_task",
                },
            },
        )

        assert result == {"decision": "accept"}
        mock_hm.handle.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_approval_safe_mcp_proxy_hook_error_still_accepts(self) -> None:
        """Safe MCP proxy discovery should fail open when hook processing crashes."""
        mock_hm = MagicMock()
        mock_hm.handle.side_effect = RuntimeError("Handler crashed")
        adapter = CodexAdapter(hook_manager=mock_hm)

        result = await adapter.handle_approval_request(
            "item/mcpToolCall/requestApproval",
            {
                "threadId": "thr-mcp-safe",
                "itemId": "item-mcp-safe",
                "name": "mcp__gobby__get_tool_schema",
                "arguments": {
                    "server_name": "gobby-tasks",
                    "tool_name": "create_task",
                },
            },
        )

        assert result == {"decision": "accept"}
        mock_hm.handle.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_approval_auto_accepts_safe_mcp_elicitation(self) -> None:
        """Safe MCP elicitation prompts should still fire hooks and force accept."""
        mock_hm = MagicMock()
        mock_hm.handle.return_value = HookResponse(decision="deny")
        adapter = CodexAdapter(hook_manager=mock_hm)

        result = await adapter.handle_approval_request(
            "mcpServer/elicitation/request",
            {
                "threadId": "thr-mcp-safe",
                "turnId": "turn-1",
                "serverName": "gobby",
                "mode": "form",
                "message": 'Allow the gobby MCP server to run tool "list_mcp_servers"?',
                "requestedSchema": {"type": "object", "properties": {}},
                "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": {}},
            },
        )

        assert result == {"action": "accept", "content": None, "_meta": None}
        mock_hm.handle.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_approval_auto_accepts_safe_canvas_calls(self) -> None:
        """UI-only canvas tools should still fire hooks and force accept."""
        mock_hm = MagicMock()
        mock_hm.handle.return_value = HookResponse(decision="block")
        adapter = CodexAdapter(hook_manager=mock_hm)

        result = await adapter.handle_approval_request(
            "item/mcpToolCall/requestApproval",
            {
                "threadId": "thr-canvas-safe",
                "itemId": "item-canvas-safe",
                "name": "mcp__gobby__call_tool",
                "arguments": {
                    "server_name": "gobby-canvas",
                    "tool_name": "render_surface",
                },
            },
        )

        assert result == {"decision": "accept"}
        mock_hm.handle.assert_called_once()


class TestCodexAdapterApprovalAttach:
    """Tests for approval handler registration during adapter attach."""

    def test_attach_registers_approval_handler(self) -> None:
        """Attaching adapter to client registers approval handler."""
        mock_hm = MagicMock()
        adapter = CodexAdapter(hook_manager=mock_hm)
        mock_client = MagicMock()

        adapter.attach_to_client(mock_client)

        mock_client.register_approval_handler.assert_called_once()
        assert mock_client.register_approval_handler.call_count == 1
        assert mock_client.register_approval_handler.call_args is not None


# =============================================================================
# Phase 2: Context Injection Tests
#
# Tests for injecting session metadata and workflow context into Codex turns.
# Codex uses turn-start injection: context is prepended to the `instructions`
# field when starting a turn, unlike Claude/Gemini which use per-hook
# additionalContext.
# =============================================================================


class TestCodexClientContextPrefixParameter:
    """Tests for context_prefix parameter in start_turn().

    The client should accept a context_prefix string and prepend it to
    the instructions field in the turn/start JSON-RPC request.
    """

    @pytest.mark.asyncio
    async def test_start_turn_without_context_prefix(self) -> None:
        """start_turn without context_prefix sends no instructions field."""
        client = CodexAppServerClient()

        mock_result = {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value=mock_result
        ) as mock_send:
            await client.start_turn("thr-1", "Help me refactor")

            params = mock_send.call_args[0][1]
            # No instructions field when no context_prefix
            assert "instructions" not in params

    @pytest.mark.asyncio
    async def test_start_turn_with_context_prefix(self) -> None:
        """start_turn with context_prefix adds instructions field."""
        client = CodexAppServerClient()

        mock_result = {"turn": {"id": "turn-2", "status": "inProgress", "items": []}}

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value=mock_result
        ) as mock_send:
            await client.start_turn(
                "thr-1",
                "Help me refactor",
                context_prefix="Gobby Session ID: #42",
            )

            params = mock_send.call_args[0][1]
            assert "instructions" in params
            assert "Gobby Session ID: #42" in params["instructions"]

    @pytest.mark.asyncio
    async def test_start_turn_context_prefix_none_omits_instructions(self) -> None:
        """start_turn with context_prefix=None sends no instructions field."""
        client = CodexAppServerClient()

        mock_result = {"turn": {"id": "turn-3", "status": "inProgress", "items": []}}

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value=mock_result
        ) as mock_send:
            await client.start_turn(
                "thr-1",
                "Help me refactor",
                context_prefix=None,
            )

            params = mock_send.call_args[0][1]
            assert "instructions" not in params

    @pytest.mark.asyncio
    async def test_start_turn_context_prefix_empty_string_omits_instructions(self) -> None:
        """start_turn with empty context_prefix sends no instructions field."""
        client = CodexAppServerClient()

        mock_result = {"turn": {"id": "turn-4", "status": "inProgress", "items": []}}

        with patch.object(
            client, "_send_request", new_callable=AsyncMock, return_value=mock_result
        ) as mock_send:
            await client.start_turn(
                "thr-1",
                "Help me refactor",
                context_prefix="",
            )

            params = mock_send.call_args[0][1]
            assert "instructions" not in params


class TestCodexAdapterContextStringBuilding:
    """Tests for context string building in CodexAdapter.

    The adapter should build context strings from HookResponse metadata,
    similar to Claude/Gemini adapters, for injection into Codex turns.
    translate_from_hook_response() should include context for BEFORE_AGENT hooks.
    """

    def test_translate_response_includes_context(self) -> None:
        """translate_from_hook_response includes context from HookResponse."""
        adapter = CodexAdapter()

        response = HookResponse(
            decision="allow",
            context="Workflow step: implement-code",
        )
        result = adapter.translate_from_hook_response(response)

        assert "context" in result
        assert "Workflow step: implement-code" in result["context"]

    def test_translate_response_includes_session_metadata(self) -> None:
        """translate_from_hook_response includes session metadata for first hook."""
        adapter = CodexAdapter()

        response = HookResponse(
            decision="allow",
            metadata={
                "session_id": "plat-uuid-123",
                "session_ref": "#42",
                "external_id": "thr-codex-abc",
                "_first_hook_for_session": True,
            },
        )
        result = adapter.translate_from_hook_response(response)

        assert "context" in result
        context = result["context"]
        assert "Gobby Session ID:" in context
        assert "#42" in context

    def test_translate_response_no_metadata_on_subsequent_hooks(self) -> None:
        """Subsequent hooks do not inject session ref."""
        adapter = CodexAdapter()

        response = HookResponse(
            decision="allow",
            metadata={
                "session_id": "plat-uuid-123",
                "session_ref": "#42",
                "_first_hook_for_session": False,
            },
        )
        result = adapter.translate_from_hook_response(response)

        assert "context" not in result

    def test_translate_response_no_context_when_no_metadata(self) -> None:
        """No context field when no metadata or context."""
        adapter = CodexAdapter()

        response = HookResponse(decision="allow")
        result = adapter.translate_from_hook_response(response)

        # Should only have decision field
        assert result == {"decision": "accept"}

    def test_translate_response_first_hook_full_metadata(self) -> None:
        """First hook includes full session metadata (project, machine, etc.)."""
        adapter = CodexAdapter()

        response = HookResponse(
            decision="allow",
            metadata={
                "session_id": "plat-uuid-456",
                "session_ref": "#99",
                "external_id": "thr-codex-xyz",
                "machine_id": "machine-abc",
                "project_id": "proj-def",
                "_first_hook_for_session": True,
            },
        )
        result = adapter.translate_from_hook_response(response)

        context = result["context"]
        assert "#99" in context
        assert "plat-uuid-456" in context
        assert "thr-codex-xyz" in context
        assert "machine-abc" in context
        assert "proj-def" in context

    def test_translate_response_combines_context_and_metadata(self) -> None:
        """Both workflow context and session metadata are combined."""
        adapter = CodexAdapter()

        response = HookResponse(
            decision="allow",
            context="Active workflow: auto-task",
            metadata={
                "session_id": "plat-uuid-789",
                "session_ref": "#100",
                "_first_hook_for_session": True,
            },
        )
        result = adapter.translate_from_hook_response(response)

        context = result["context"]
        assert "Active workflow: auto-task" in context
        assert "Gobby Session ID:" in context
        assert "#100" in context


class TestCodexAdapterContextOneTimeInjection:
    """Tests for one-time context injection behavior.

    Context metadata should only be injected fully on the first hook
    per session. Subsequent hooks should only include minimal session ref.
    This matches the behavior in Claude/Gemini adapters.
    """

    def test_first_hook_flag_controls_metadata_depth(self) -> None:
        """_first_hook_for_session=True triggers full metadata injection."""
        adapter = CodexAdapter()

        # First hook - full metadata
        first_response = HookResponse(
            decision="allow",
            metadata={
                "session_id": "plat-1",
                "session_ref": "#50",
                "external_id": "thr-ext-1",
                "machine_id": "m-1",
                "_first_hook_for_session": True,
            },
        )
        first_result = adapter.translate_from_hook_response(first_response)

        # Subsequent hook - minimal metadata
        subsequent_response = HookResponse(
            decision="allow",
            metadata={
                "session_id": "plat-1",
                "session_ref": "#50",
                "external_id": "thr-ext-1",
                "machine_id": "m-1",
                "_first_hook_for_session": False,
            },
        )
        subsequent_result = adapter.translate_from_hook_response(subsequent_response)

        # First should be fuller than subsequent
        first_context = first_result.get("context", "")
        subsequent_context = subsequent_result.get("context", "")

        # First has full metadata
        assert "thr-ext-1" in first_context
        # Subsequent has no context at all
        assert subsequent_context == ""

    def test_no_session_id_means_no_context_injection(self) -> None:
        """Without session_id in metadata, no context is injected."""
        adapter = CodexAdapter()

        response = HookResponse(
            decision="allow",
            metadata={
                "_first_hook_for_session": True,
                # No session_id
            },
        )
        result = adapter.translate_from_hook_response(response)

        # No context injected without session_id
        assert result == {"decision": "accept"}


class TestCodexAdapterContextFormat:
    """Tests for context format consistency with Claude/Gemini adapters.

    The Codex adapter should produce context strings that follow the same
    patterns as Claude and Gemini adapters for consistency across CLIs.
    """

    def test_session_ref_format(self) -> None:
        """Session ref uses '#N' format in context."""
        adapter = CodexAdapter()

        response = HookResponse(
            decision="allow",
            metadata={
                "session_id": "uuid-123",
                "session_ref": "#77",
                "_first_hook_for_session": True,
            },
        )
        result = adapter.translate_from_hook_response(response)

        context = result["context"]
        # Should contain "Gobby Session ID: #77" similar to Claude/Gemini
        assert "Gobby Session ID: #77" in context

    def test_session_ref_with_full_id(self) -> None:
        """First hook shows both session ref and full UUID."""
        adapter = CodexAdapter()

        response = HookResponse(
            decision="allow",
            metadata={
                "session_id": "uuid-full-456",
                "session_ref": "#88",
                "_first_hook_for_session": True,
            },
        )
        result = adapter.translate_from_hook_response(response)

        context = result["context"]
        # Should include both ref and full ID like Claude adapter
        assert "#88" in context
        assert "uuid-full-456" in context

    def test_external_id_labeled(self) -> None:
        """External ID is labeled as CLI-specific session ID."""
        adapter = CodexAdapter()

        response = HookResponse(
            decision="allow",
            metadata={
                "session_id": "plat-id",
                "session_ref": "#10",
                "external_id": "thr-codex-external",
                "_first_hook_for_session": True,
            },
        )
        result = adapter.translate_from_hook_response(response)

        context = result["context"]
        assert "thr-codex-external" in context

    def test_decision_still_present_with_context(self) -> None:
        """Decision field is always present alongside context."""
        adapter = CodexAdapter()

        response = HookResponse(
            decision="deny",
            metadata={
                "session_id": "plat-deny",
                "session_ref": "#5",
                "_first_hook_for_session": True,
            },
        )
        result = adapter.translate_from_hook_response(response)

        assert result["decision"] == "decline"
        assert "context" in result


# =============================================================================
# Phase 3: Workflow Enforcement Tests
#
# Tests for tool name normalization working correctly with Codex events.
# The workflow enforcement engine uses event_data["tool_name"] which must
# be normalized from Codex-native names (commandExecution, fileChange)
# to CC-style names (Bash, Write).
# =============================================================================


class TestCodexToolNameNormalization:
    """Tests for Codex tool name normalization via TOOL_MAP.

    The adapter normalizes Codex tool names to Claude Code conventions
    so block_tools rules work consistently across CLIs.
    """

    def test_command_execution_maps_to_bash(self) -> None:
        """commandExecution normalizes to Bash."""
        adapter = CodexAdapter()
        assert adapter.normalize_tool_name("commandExecution") == "Bash"

    def test_read_file_variants_map_to_read(self) -> None:
        """read_file and ReadFile normalize to Read."""
        adapter = CodexAdapter()
        assert adapter.normalize_tool_name("read_file") == "Read"
        assert adapter.normalize_tool_name("ReadFile") == "Read"

    def test_write_file_variants_map_to_write(self) -> None:
        """write_file and WriteFile normalize to Write."""
        adapter = CodexAdapter()
        assert adapter.normalize_tool_name("write_file") == "Write"
        assert adapter.normalize_tool_name("WriteFile") == "Write"

    def test_edit_file_variants_map_to_edit(self) -> None:
        """edit_file and EditFile normalize to Edit."""
        adapter = CodexAdapter()
        assert adapter.normalize_tool_name("edit_file") == "Edit"
        assert adapter.normalize_tool_name("EditFile") == "Edit"

    def test_shell_variants_map_to_bash(self) -> None:
        """Shell command variants normalize to Bash."""
        adapter = CodexAdapter()
        assert adapter.normalize_tool_name("run_shell_command") == "Bash"
        assert adapter.normalize_tool_name("RunShellCommand") == "Bash"

    def test_search_tools_normalize(self) -> None:
        """Search tools normalize to Glob/Grep."""
        adapter = CodexAdapter()
        assert adapter.normalize_tool_name("glob") == "Glob"
        assert adapter.normalize_tool_name("grep") == "Grep"
        assert adapter.normalize_tool_name("GlobTool") == "Glob"
        assert adapter.normalize_tool_name("GrepTool") == "Grep"

    def test_unknown_tool_passes_through(self) -> None:
        """Unknown tool names pass through unchanged."""
        adapter = CodexAdapter()
        assert adapter.normalize_tool_name("customTool") == "customTool"
        assert adapter.normalize_tool_name("myPlugin") == "myPlugin"

    def test_approval_event_uses_normalized_names(self) -> None:
        """Approval events produce normalized tool names in HookEvent."""
        adapter = CodexAdapter()

        # commandExecution should produce "Bash"
        event = adapter._translate_approval_event(
            "item/commandExecution/requestApproval",
            {"threadId": "thr-1", "itemId": "item-1", "parsedCmd": "ls"},
        )
        assert event is not None
        assert event.data["tool_name"] == "Bash"

        # fileChange should produce "Write"
        event = adapter._translate_approval_event(
            "item/fileChange/requestApproval",
            {"threadId": "thr-1", "itemId": "item-2", "changes": []},
        )
        assert event is not None
        assert event.data["tool_name"] == "Write"

        # mcpToolCall should preserve the raw MCP tool identity
        event = adapter._translate_approval_event(
            "item/mcpToolCall/requestApproval",
            {
                "threadId": "thr-1",
                "itemId": "item-3",
                "name": "mcp__gobby__get_tool_schema",
                "arguments": {"server_name": "gobby-tasks", "tool_name": "claim_task"},
            },
        )
        assert event is not None
        assert event.data["tool_name"] == "mcp__gobby__get_tool_schema"
        assert event.data["mcp_server"] == "gobby"
        assert event.data["mcp_tool"] == "get_tool_schema"


class TestCodexApprovalDeclineFormat:
    """Tests for approval decline response format from Codex adapter.

    When a tool is blocked, the adapter must translate the HookResponse
    with decision="deny" into Codex's {"decision": "decline"} format.
    This ensures the Codex agent receives a proper denial via JSON-RPC.
    """

    @pytest.mark.asyncio
    async def test_blocked_tool_produces_decline(self) -> None:
        """HookManager deny → adapter decline for Codex."""
        mock_hm = MagicMock()
        mock_hm.handle.return_value = HookResponse(
            decision="deny",
            reason="Bash is blocked in this workflow step.",
        )
        adapter = CodexAdapter(hook_manager=mock_hm)

        result = await adapter.handle_approval_request(
            "item/commandExecution/requestApproval",
            {"threadId": "thr-blocked", "parsedCmd": "rm -rf /"},
        )

        assert result == {"decision": "decline"}

    @pytest.mark.asyncio
    async def test_allowed_tool_produces_accept(self) -> None:
        """HookManager allow → adapter accept for Codex."""
        mock_hm = MagicMock()
        mock_hm.handle.return_value = HookResponse(decision="allow")
        adapter = CodexAdapter(hook_manager=mock_hm)

        result = await adapter.handle_approval_request(
            "item/commandExecution/requestApproval",
            {"threadId": "thr-ok", "parsedCmd": "echo hello"},
        )

        assert result == {"decision": "accept"}

    @pytest.mark.asyncio
    async def test_hook_error_defaults_to_decline(self) -> None:
        """Hook processing errors fail closed."""
        mock_hm = MagicMock()
        mock_hm.handle.side_effect = RuntimeError("Handler crashed")
        adapter = CodexAdapter(hook_manager=mock_hm)

        result = await adapter.handle_approval_request(
            "item/commandExecution/requestApproval",
            {"threadId": "thr-err", "parsedCmd": "ls"},
        )

        assert result == {"decision": "decline"}

    @pytest.mark.asyncio
    async def test_unknown_approval_method_fails_closed(self) -> None:
        """Unknown approval methods fail closed instead of auto-accepting."""
        adapter = CodexAdapter(hook_manager=MagicMock())

        result = await adapter.handle_approval_request(
            "item/unknown/requestApproval",
            {"threadId": "thr-unknown"},
        )

        assert result == {"decision": "decline"}

    @pytest.mark.asyncio
    async def test_missing_hook_manager_fails_closed(self) -> None:
        """Approval requests fail closed when no hook manager is configured."""
        adapter = CodexAdapter(hook_manager=None)

        result = await adapter.handle_approval_request(
            "item/commandExecution/requestApproval",
            {"threadId": "thr-nohm", "parsedCmd": "ls"},
        )

        assert result == {"decision": "decline"}

    @pytest.mark.asyncio
    async def test_mcp_elicitation_failures_cancel(self) -> None:
        """MCP elicitation requests fail closed with cancel."""
        mock_hm = MagicMock()
        mock_hm.handle.side_effect = RuntimeError("Handler crashed")
        adapter = CodexAdapter(hook_manager=mock_hm)

        result = await adapter.handle_approval_request(
            "mcpServer/elicitation/request",
            {
                "threadId": "thr-mcp",
                "turnId": "turn-1",
                "serverName": "gobby",
                "mode": "form",
                "message": 'Allow the gobby MCP server to run tool "call_tool"?',
                "requestedSchema": {"type": "object", "properties": {}},
                "_meta": {
                    "codex_approval_kind": "mcp_tool_call",
                    "tool_params": {"server_name": "gobby-tasks", "tool_name": "claim_task"},
                },
            },
        )

        assert result == {"action": "cancel", "content": None, "_meta": None}

    def test_translate_block_to_decline(self) -> None:
        """translate_from_hook_response maps 'block' decision correctly."""
        adapter = CodexAdapter()

        # 'deny' maps to 'decline'
        response = HookResponse(decision="deny")
        result = adapter.translate_from_hook_response(response)
        assert result["decision"] == "decline"

    def test_translate_allow_to_accept(self) -> None:
        """translate_from_hook_response maps 'allow' to 'accept'."""
        adapter = CodexAdapter()

        response = HookResponse(decision="allow")
        result = adapter.translate_from_hook_response(response)
        assert result["decision"] == "accept"


class TestCodexWorkflowEnforcementIntegration:
    """Integration tests verifying end-to-end workflow enforcement for Codex.

    These tests verify that the full chain works:
    1. Codex sends approval request
    2. Adapter translates to HookEvent with normalized tool name
    3. HookManager evaluates workflow rules (block_tools)
    4. Adapter translates HookResponse back to Codex format
    """

    @pytest.mark.asyncio
    async def test_full_chain_blocked_command(self) -> None:
        """Full chain: Codex approval → HookEvent → deny → decline."""
        mock_hm = MagicMock()
        mock_hm.handle.return_value = HookResponse(
            decision="deny",
            reason="Tool blocked by workflow",
        )
        adapter = CodexAdapter(hook_manager=mock_hm)

        # Simulate Codex sending commandExecution approval
        result = await adapter.handle_approval_request(
            "item/commandExecution/requestApproval",
            {
                "threadId": "thr-chain",
                "itemId": "item-chain",
                "turnId": "turn-1",
                "parsedCmd": "pip install malware",
                "reason": "tool use",
                "risk": "high",
            },
        )

        # Verify HookEvent was created correctly
        hook_event = mock_hm.handle.call_args[0][0]
        assert hook_event.event_type == HookEventType.BEFORE_TOOL
        assert hook_event.data["tool_name"] == "Bash"
        assert hook_event.data["tool_input"] == "pip install malware"
        assert hook_event.source == SessionSource.CODEX

        # Verify decline response
        assert result == {"decision": "decline"}

    @pytest.mark.asyncio
    async def test_full_chain_allowed_file_change(self) -> None:
        """Full chain: Codex file change → HookEvent → allow → accept."""
        mock_hm = MagicMock()
        mock_hm.handle.return_value = HookResponse(decision="allow")
        adapter = CodexAdapter(hook_manager=mock_hm)

        changes = [{"path": "/src/app.py", "content": "print('hello')"}]
        result = await adapter.handle_approval_request(
            "item/fileChange/requestApproval",
            {
                "threadId": "thr-file",
                "itemId": "item-file",
                "changes": changes,
            },
        )

        hook_event = mock_hm.handle.call_args[0][0]
        assert hook_event.data["tool_name"] == "Write"
        assert hook_event.data["tool_input"] == {
            "changes": changes,
            "file_path": "/src/app.py",
        }

        assert result == {"decision": "accept"}

    @pytest.mark.asyncio
    async def test_full_chain_allowed_mcp_call(self) -> None:
        """Full chain: non-safe Codex MCP approval → HookEvent → allow → accept."""
        mock_hm = MagicMock()
        mock_hm.handle.return_value = HookResponse(decision="allow")
        adapter = CodexAdapter(hook_manager=mock_hm)

        result = await adapter.handle_approval_request(
            "item/mcpToolCall/requestApproval",
            {
                "threadId": "thr-mcp",
                "itemId": "item-mcp",
                "name": "mcp__gobby__call_tool",
                "arguments": {"server_name": "gobby-tasks", "tool_name": "claim_task"},
            },
        )

        hook_event = mock_hm.handle.call_args[0][0]
        assert hook_event.event_type == HookEventType.BEFORE_TOOL
        assert hook_event.data["tool_name"] == "mcp__gobby__call_tool"
        assert hook_event.data["tool_input"] == {
            "server_name": "gobby-tasks",
            "tool_name": "claim_task",
        }
        assert hook_event.data["mcp_server"] == "gobby-tasks"
        assert hook_event.data["mcp_tool"] == "claim_task"
        assert hook_event.source == SessionSource.CODEX

        assert result == {"decision": "accept"}

    @pytest.mark.asyncio
    async def test_full_chain_allowed_mcp_elicitation(self) -> None:
        """Full chain: Codex MCP elicitation → HookEvent → allow → accept."""
        mock_hm = MagicMock()
        mock_hm.handle.return_value = HookResponse(decision="allow")
        adapter = CodexAdapter(hook_manager=mock_hm)

        result = await adapter.handle_approval_request(
            "mcpServer/elicitation/request",
            {
                "threadId": "thr-mcp",
                "turnId": "turn-1",
                "serverName": "gobby",
                "mode": "form",
                "message": 'Allow the gobby MCP server to run tool "call_tool"?',
                "requestedSchema": {"type": "object", "properties": {}},
                "_meta": {
                    "codex_approval_kind": "mcp_tool_call",
                    "tool_params": {"server_name": "gobby-tasks", "tool_name": "claim_task"},
                },
            },
        )

        hook_event = mock_hm.handle.call_args[0][0]
        assert hook_event.event_type == HookEventType.BEFORE_TOOL
        assert hook_event.data["tool_name"] == "mcp__gobby__call_tool"
        assert hook_event.data["tool_input"] == {
            "server_name": "gobby-tasks",
            "tool_name": "claim_task",
        }
        assert hook_event.data["mcp_server"] == "gobby-tasks"
        assert hook_event.data["mcp_tool"] == "claim_task"
        assert hook_event.source == SessionSource.CODEX

        assert result == {"action": "accept", "content": None, "_meta": None}
