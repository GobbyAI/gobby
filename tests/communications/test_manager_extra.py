"""Extra tests for gobby.communications.manager."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.communications.adapters.base import BaseChannelAdapter
from gobby.communications.manager import CommunicationsManager
from gobby.communications.models import ChannelConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_store() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_secret_store() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_session_store() -> MagicMock:
    return MagicMock()


@pytest.fixture
def manager(mock_store, mock_secret_store, mock_session_store) -> CommunicationsManager:
    config = MagicMock()
    config.channel_defaults.rate_limit_per_minute = 60
    config.channel_defaults.burst = 10
    config.webhook_base_url = None
    mgr = CommunicationsManager(config, mock_store, mock_secret_store, mock_session_store)
    return mgr


class TestAttachments:
    @pytest.fixture
    def mock_adapter(self, manager: CommunicationsManager) -> AsyncMock:
        adapter = AsyncMock(spec=BaseChannelAdapter)
        adapter.send_attachment.return_value = "plat_attach_123"
        channel = ChannelConfig(
            id="chan-id",
            channel_type="test",
            name="test_channel",
            enabled=True,
            config_json={},
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )
        manager._adapters["test_channel"] = adapter
        manager._channel_by_name["test_channel"] = channel
        return adapter

    @pytest.mark.asyncio
    async def test_send_attachment_success(self, manager, mock_adapter, tmp_path):
        file_path = tmp_path / "test.txt"
        file_path.write_text("hello")

        msg, attachment = await manager.send_attachment(
            "test_channel", file_path, filename="hello.txt", content="Here is a file."
        )

        assert msg.status == "sent"
        assert msg.platform_message_id == "plat_attach_123"
        assert attachment.filename == "hello.txt"
        assert attachment.size_bytes == 5

        manager._store.create_message.assert_called_once()
        manager._store.create_attachment.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_attachment_file_not_found(self, manager, tmp_path):
        with pytest.raises(ValueError, match="Attachment file not found"):
            await manager.send_attachment("test_channel", tmp_path / "missing.txt")

    @pytest.mark.asyncio
    async def test_send_attachment_size_exceeded(self, manager, mock_adapter, tmp_path):
        file_path = tmp_path / "large.txt"
        # Since we use fake file sizes, let's mock the stat
        manager.attachment_manager.validate_size = MagicMock(return_value=False)
        manager.attachment_manager.get_size_limit = MagicMock(return_value=100)
        file_path.write_text("x" * 150)

        with pytest.raises(ValueError, match="exceeds test limit"):
            await manager.send_attachment("test_channel", file_path)

    @pytest.mark.asyncio
    async def test_send_attachment_not_supported(self, manager, mock_adapter, tmp_path):
        file_path = tmp_path / "test.txt"
        file_path.write_text("hello")

        mock_adapter.send_attachment.side_effect = NotImplementedError("no attachments")
        manager.event_callback = AsyncMock()

        msg, attachment = await manager.send_attachment("test_channel", file_path)

        assert msg.status == "failed"
        assert "not support file attachments" in msg.error

        # We still store failed message/attachment
        manager._store.create_attachment.assert_called_once()
        manager.event_callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_attachment_exception(self, manager, mock_adapter, tmp_path):
        file_path = tmp_path / "test.txt"
        file_path.write_text("hello")

        mock_adapter.send_attachment.side_effect = Exception("network error")

        msg, _ = await manager.send_attachment("test_channel", file_path)
        assert msg.status == "failed"
        assert "network error" in str(msg.error)


class TestGobbyChatAutoCreate:
    @pytest.mark.asyncio
    @patch("gobby.communications.manager.get_adapter_class")
    async def test_ensure_gobby_chat_channel_creates(self, mock_get_adapter, manager):
        manager._store.list_channels.return_value = []
        mock_get_adapter.return_value = MagicMock()  # something is registered

        await manager._ensure_gobby_chat_channel()

        # created gobby_chat channel
        manager._store.create_channel.assert_called_once()
        created = manager._store.create_channel.call_args[0][0]
        assert created.channel_type == "gobby_chat"
        assert created.name == "gobby_chat"

    @pytest.mark.asyncio
    async def test_ensure_gobby_chat_channel_already_exists(self, manager):
        mock_channel = MagicMock()
        mock_channel.channel_type = "gobby_chat"
        manager._store.list_channels.return_value = [mock_channel]

        await manager._ensure_gobby_chat_channel()

        manager._store.create_channel.assert_not_called()
        assert manager._store.create_channel.call_count == 0
        assert not manager._store.create_channel.called

    def test_set_websocket_broadcast(self, manager):
        from gobby.communications.adapters.gobby_chat import GobbyChatAdapter

        adapter_instance = MagicMock(spec=GobbyChatAdapter)
        manager._adapters["gobby_chat"] = adapter_instance

        mock_broadcast = MagicMock()
        manager.set_websocket_broadcast(mock_broadcast)

        adapter_instance.set_broadcast.assert_called_once_with(mock_broadcast)
        assert adapter_instance.set_broadcast.call_count == 1
        assert adapter_instance.set_broadcast.call_args is not None


class TestDelegates:
    """Test simple delegation methods to hit coverage lines."""

    def test_list_messages(self, manager):
        manager._store.list_messages.return_value = []
        res = manager.list_messages(limit=10)
        assert res == []
        manager._store.list_messages.assert_called_once_with(
            channel_id=None, session_id=None, direction=None, limit=10, offset=0
        )

    def test_get_identity_by_external(self, manager):
        manager.get_identity_by_external("chan1", "ext1")
        manager._store.get_identity_by_external.assert_called_with("chan1", "ext1")
        assert manager._store.get_identity_by_external.call_count >= 1
        assert manager._store.get_identity_by_external.call_args is not None

    def test_list_identities(self, manager):
        manager.list_identities("chan1")
        manager._store.list_identities.assert_called_with(channel_id="chan1")
        assert manager._store.list_identities.call_count >= 1
        assert manager._store.list_identities.call_args is not None

    def test_find_cross_channel_identity(self, manager):
        manager._identity_manager.find_cross_channel_identity = MagicMock(return_value="id_1")
        assert manager._find_cross_channel_identity("user1") == "id_1"

    def test_bridge_identity(self, manager):
        manager._identity_manager.bridge_identity = MagicMock()
        manager._bridge_identity("id_1", "sess_1")
        manager._identity_manager.bridge_identity.assert_called_with("id_1", "sess_1")
        assert manager._identity_manager.bridge_identity.call_count >= 1
        assert manager._identity_manager.bridge_identity.call_args is not None
