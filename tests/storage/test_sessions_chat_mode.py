"""Tests for chat_mode persistence in sessions."""

import pytest

from gobby.storage.sessions import LocalSessionManager

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000060887"  # _personal


@pytest.fixture
def sm(session_manager: LocalSessionManager) -> LocalSessionManager:
    return session_manager


class TestChatModePersistence:
    """Verify chat_mode column in sessions table."""

    def test_default_value_on_create(self, sm: LocalSessionManager) -> None:
        """New sessions should default to chat_mode='plan'."""
        session = sm.register(
            external_id="ext-mode-default",
            machine_id="m1",
            source="test",
            project_id=PROJECT_ID,
        )
        assert session.chat_mode == "plan"

    def test_update_chat_mode(self, sm: LocalSessionManager) -> None:
        """update_chat_mode should persist the value."""
        session = sm.register(
            external_id="ext-mode-update",
            machine_id="m1",
            source="test",
            project_id=PROJECT_ID,
        )
        sm.update_chat_mode(session.id, "bypass")

        reloaded = sm.get(session.id)
        assert reloaded is not None
        assert reloaded.chat_mode == "bypass"

    def test_survives_register_reconnect(self, sm: LocalSessionManager) -> None:
        """chat_mode should survive a register() reconnect (daemon restart)."""
        session = sm.register(
            external_id="ext-mode-reconnect",
            machine_id="m1",
            source="test",
            project_id=PROJECT_ID,
        )
        sm.update_chat_mode(session.id, "accept_edits")

        # Simulate daemon restart — register() with same external_id
        reconnected = sm.register(
            external_id="ext-mode-reconnect",
            machine_id="m1",
            source="test",
            project_id=PROJECT_ID,
        )
        assert reconnected.id == session.id
        assert reconnected.chat_mode == "accept_edits"

    def test_to_dict_includes_chat_mode(self, sm: LocalSessionManager) -> None:
        """to_dict() should include chat_mode."""
        session = sm.register(
            external_id="ext-mode-dict",
            machine_id="m1",
            source="test",
            project_id=PROJECT_ID,
        )
        sm.update_chat_mode(session.id, "normal")
        reloaded = sm.get(session.id)
        assert reloaded is not None

        d = reloaded.to_dict()
        assert d["chat_mode"] == "normal"

    def test_invalid_mode_raises(self, sm: LocalSessionManager) -> None:
        """Invalid chat_mode values should raise ValueError."""
        session = sm.register(
            external_id="ext-mode-invalid",
            machine_id="m1",
            source="test",
            project_id=PROJECT_ID,
        )
        with pytest.raises(ValueError, match="Invalid chat_mode"):
            sm.update_chat_mode(session.id, "turbo")

    def test_all_modes(self, sm: LocalSessionManager) -> None:
        """All valid modes should round-trip through the DB."""
        session = sm.register(
            external_id="ext-mode-all",
            machine_id="m1",
            source="test",
            project_id=PROJECT_ID,
        )
        for mode in ("plan", "accept_edits", "normal", "bypass"):
            sm.update_chat_mode(session.id, mode)
            reloaded = sm.get(session.id)
            assert reloaded is not None
            assert reloaded.chat_mode == mode
