"""ACP plan-approval mode switch (1c, #15616).

ACP CLIs expose no protocol-level mode push (no session/set_mode), so plan
approval relies on a user-visible fallback in
``ACPWebChatPermissionsMixin.sync_sdk_permission_mode``: broadcast
``mode_changed`` reason ``plan_approved`` (so the UI Plan radio switches off)
and stop the plan-mode gate from re-injecting on the next prompt. It must NOT
fire on a plain manual mode switch.
"""

from __future__ import annotations

from gobby.servers.websocket.chat.backends.acp_session import ACPManagedChatSession


def _make_session() -> tuple[ACPManagedChatSession, list[tuple[str, str]]]:
    session = ACPManagedChatSession(conversation_id="conv-1")
    session.chat_mode = "plan"
    mode_changes: list[tuple[str, str]] = []

    async def _on_mode_changed(mode: str, reason: str) -> None:
        mode_changes.append((mode, reason))

    session._on_mode_changed = _on_mode_changed
    return session, mode_changes


async def test_approve_flips_mode_broadcasts_and_clears_plan_gate() -> None:
    session, mode_changes = _make_session()
    # A plan is pending (as 1b's send_message hook would have surfaced it).
    session._pending_plan_content = "## Plan\n1. do it"
    assert session.has_pending_plan is True

    # Mirror handle_plan_approval_response's has_pending_plan approve branch.
    session._pending_post_plan_mode = "normal"
    session.set_chat_mode("normal")
    session._clear_pending_plan_prompt()
    await session.sync_sdk_permission_mode()
    session.provide_plan_decision("approve")

    assert session.chat_mode == "normal"
    assert mode_changes == [("normal", "plan_approved")]
    assert session.has_pending_plan is False
    # The next prompt build no longer injects the plan-mode gate.
    assert session._pop_plan_mode_context() is None


async def test_manual_mode_switch_does_not_broadcast_plan_approved() -> None:
    session, mode_changes = _make_session()
    # session_config path: set_chat_mode + sync with no pending post-plan mode.
    session.set_chat_mode("normal")
    await session.sync_sdk_permission_mode()
    assert mode_changes == []


async def test_sync_while_in_plan_mode_is_noop() -> None:
    session, mode_changes = _make_session()
    session._pending_post_plan_mode = "normal"  # set, but still in plan mode
    await session.sync_sdk_permission_mode()
    assert mode_changes == []
