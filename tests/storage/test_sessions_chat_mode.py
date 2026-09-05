"""Tests for chat_mode persistence in sessions."""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000060887"  # _personal

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000005"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def sm(session_manager: SessionManager) -> SessionManager:
    return session_manager


class TestChatModePersistence:
    """Verify chat_mode column in sessions table."""

    @pytest.mark.parametrize("mode", ["normal", "accept_edits", "bypass"])
    def test_leaving_plan_rearms_directive_without_prompt(
        self, sm: SessionManager, mode: str
    ) -> None:
        session = sm.register(
            external_id="plan-period",
            machine_id=LOCAL_MACHINE_ID,
            source="test",
            project_id=PROJECT_ID,
        )
        variables = SessionVariableManager(sm.db)
        variables.merge_variables(
            session.id, {"plan_skill_directive_delivered": True, "unrelated": "preserved"}
        )
        sm.update_chat_mode(session.id, "plan")
        assert variables.get_variables(session.id)["plan_skill_directive_delivered"] is True
        sm.update_chat_mode(session.id, mode)
        sm.update_chat_mode(session.id, "plan")
        state = variables.get_variables(session.id)
        assert state["plan_skill_directive_delivered"] is False
        assert state["unrelated"] == "preserved"

    def test_default_value_on_create(self, sm: SessionManager) -> None:
        """New sessions should default to chat_mode='plan'."""
        session = sm.register(
            external_id="ext-mode-default",
            machine_id="21000000-0000-4000-8000-000000000005",
            source="test",
            project_id=PROJECT_ID,
        )
        assert session.chat_mode == "plan"

    def test_update_chat_mode(self, sm: SessionManager) -> None:
        """update_chat_mode should persist the value."""
        session = sm.register(
            external_id="ext-mode-update",
            machine_id="21000000-0000-4000-8000-000000000005",
            source="test",
            project_id=PROJECT_ID,
        )
        sm.update_chat_mode(session.id, "bypass")

        reloaded = sm.get(session.id)
        assert reloaded is not None
        assert reloaded.chat_mode == "bypass"

    def test_survives_register_reconnect(self, sm: SessionManager) -> None:
        """chat_mode should survive a register() reconnect (daemon restart)."""
        session = sm.register(
            external_id="ext-mode-reconnect",
            machine_id="21000000-0000-4000-8000-000000000005",
            source="test",
            project_id=PROJECT_ID,
        )
        sm.update_chat_mode(session.id, "accept_edits")

        # Simulate daemon restart — register() with same external_id
        reconnected = sm.register(
            external_id="ext-mode-reconnect",
            machine_id="21000000-0000-4000-8000-000000000005",
            source="test",
            project_id=PROJECT_ID,
        )
        assert reconnected.id == session.id
        assert reconnected.chat_mode == "accept_edits"

    def test_to_dict_includes_chat_mode(self, sm: SessionManager) -> None:
        """to_dict() should include chat_mode."""
        session = sm.register(
            external_id="ext-mode-dict",
            machine_id="21000000-0000-4000-8000-000000000005",
            source="test",
            project_id=PROJECT_ID,
        )
        sm.update_chat_mode(session.id, "normal")
        reloaded = sm.get(session.id)
        assert reloaded is not None

        d = reloaded.to_dict()
        assert d["chat_mode"] == "normal"

    def test_invalid_mode_raises(self, sm: SessionManager) -> None:
        """Invalid chat_mode values should raise ValueError."""
        session = sm.register(
            external_id="ext-mode-invalid",
            machine_id="21000000-0000-4000-8000-000000000005",
            source="test",
            project_id=PROJECT_ID,
        )
        with pytest.raises(ValueError, match="Invalid chat_mode"):
            sm.update_chat_mode(session.id, "turbo")

    def test_all_modes(self, sm: SessionManager) -> None:
        """All valid modes should round-trip through the DB."""
        session = sm.register(
            external_id="ext-mode-all",
            machine_id="21000000-0000-4000-8000-000000000005",
            source="test",
            project_id=PROJECT_ID,
        )
        for mode in ("plan", "accept_edits", "normal", "bypass"):
            sm.update_chat_mode(session.id, mode)
            reloaded = sm.get(session.id)
            assert reloaded is not None
            assert reloaded.chat_mode == mode
