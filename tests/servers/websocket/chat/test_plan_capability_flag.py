"""Per-CLI plan-capability flag (1e, #15618).

The session_info frame advertises `plan_auto_switch` so the UI can show
approve/reject for every managed CLI and note "manual switch required" only
where a protocol genuinely cannot auto-switch. Native (Claude SDK) defaults to
auto-switch; ACP-backed CLIs (no protocol session/set_mode) advertise False.
"""

from __future__ import annotations

from gobby.servers.gemini_permissions import GeminiWebChatPermissionsMixin
from gobby.servers.websocket.chat.acp_permissions import ACPWebChatPermissionsMixin
from gobby.servers.websocket.chat.backends.acp_session import ACPManagedChatSession


def _session_info_capability(session: object) -> bool:
    # Mirrors the session_info builder's default-native resolution.
    return bool(getattr(session, "plan_auto_switch", True))


def test_acp_cli_requires_manual_plan_switch() -> None:
    assert ACPWebChatPermissionsMixin.plan_auto_switch is False
    session = ACPManagedChatSession(conversation_id="conv-1")
    assert session.plan_auto_switch is False
    assert _session_info_capability(session) is False


def test_gemini_mixin_cli_requires_manual_plan_switch() -> None:
    # Codex/Droid use GeminiWebChatPermissionsMixin.
    assert GeminiWebChatPermissionsMixin.plan_auto_switch is False


def test_native_session_defaults_to_auto_switch() -> None:
    # The Claude SDK session carries no override; session_info defaults to
    # native auto-switch.
    class _NativeSession:
        pass

    assert _session_info_capability(_NativeSession()) is True
