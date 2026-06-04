"""Per-CLI plan-capability flag (1e, #15618).

The session_info frame advertises `plan_auto_switch` so the UI can show
approve/reject for every managed CLI and note "manual switch required" only
where a protocol genuinely cannot auto-switch. Native (Claude SDK) defaults to
auto-switch; managed CLIs (no protocol session/set_mode) advertise False.

The permission/plan helpers are unified in a single protocol-neutral
``ManagedWebChatPermissionsMixin`` (#15631), shared by the ACP sessions
(Gemini/Grok/Qwen), Codex (app-server), and Droid (stream-jsonrpc).
"""

from __future__ import annotations

from gobby.servers.websocket.chat.backends import (
    CodexManagedChatSession,
    DroidManagedChatSession,
)
from gobby.servers.websocket.chat.backends.acp_session import ACPManagedChatSession
from gobby.servers.websocket.chat.permissions import ManagedWebChatPermissionsMixin


def _session_info_capability(session: object) -> bool:
    # Mirrors the session_info builder's default-native resolution.
    return bool(getattr(session, "plan_auto_switch", True))


def test_unified_mixin_defaults_to_manual_plan_switch() -> None:
    assert ManagedWebChatPermissionsMixin.plan_auto_switch is False


def test_all_managed_sessions_share_the_unified_mixin() -> None:
    # The whole point of the unification: ACP, Codex, and Droid sessions all
    # inherit the one permission mixin — none reaches a "Gemini"-named class.
    assert issubclass(ACPManagedChatSession, ManagedWebChatPermissionsMixin)
    assert issubclass(CodexManagedChatSession, ManagedWebChatPermissionsMixin)
    assert issubclass(DroidManagedChatSession, ManagedWebChatPermissionsMixin)


def test_acp_cli_requires_manual_plan_switch() -> None:
    session = ACPManagedChatSession(conversation_id="conv-1")
    assert session.plan_auto_switch is False
    assert _session_info_capability(session) is False


def test_native_session_defaults_to_auto_switch() -> None:
    # The Claude SDK session carries no override; session_info defaults to
    # native auto-switch.
    class _NativeSession:
        pass

    assert _session_info_capability(_NativeSession()) is True
