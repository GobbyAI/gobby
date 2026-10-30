"""Target checks an allowed ``send_keys`` caller must pass before keys are written.

The bundled ``no-send-keys`` rule decides who may call ``send_keys``. This module
decides whether the target can take keys: a live agent CLI in the foreground, an
empty composer that the composer ledger vouches for, and no credential prompt on
screen. Every signal that cannot be read refuses.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from gobby.terminals.composer_ledger import read_composer
from gobby.terminals.foreground import foreground_commands, shell_pid
from gobby.terminals.pane_io import DEFAULT_SNAPSHOT_LINES, live_runtime_pane

logger = logging.getLogger(__name__)

SEND_MESSAGE_HINT = "Use gobby-agents:send_message for cross-session communication."

_SHELLS = frozenset({"ash", "bash", "csh", "dash", "fish", "ksh", "sh", "tcsh", "zsh"})
# Bare command names of the agent CLIs, as SessionSource names them. Any other foreground
# process, a shell or an editor or interpreter alike, is not a composer to type into.
_AGENT_CLIS = frozenset({"agy", "claude", "codex", "droid", "grok"})
_CREDENTIAL_PROMPT = re.compile(r"\b(password|passphrase|passcode|pin)\b[^\n]*:\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class SendKeysRefusal:
    code: str
    message: str


def _refuse(code: str, reason: str) -> SendKeysRefusal:
    return SendKeysRefusal(code, f"send_keys refused: {reason}. {SEND_MESSAGE_HINT}")


def _last_nonblank_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


async def send_keys_target_refusal(
    session_id: str,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
) -> SendKeysRefusal | None:
    """Return why the target cannot take keys, or ``None`` when it shows an idle agent composer."""
    if terminal_manager is None or terminal_runtime_registry is None:
        return _refuse("send_keys_target_unconfirmed", "the target terminal cannot be inspected")
    try:
        terminal = terminal_manager.get_live_for_session(session_id)
        if terminal is None:
            return _refuse("send_keys_no_live_terminal", "the target has no live terminal")

        pid = shell_pid(terminal)
        foreground = await asyncio.to_thread(foreground_commands, {terminal.id: pid}) if pid else {}
        command = foreground.get(terminal.id)
        if command is None:
            return _refuse(
                "send_keys_target_unconfirmed",
                "the target's foreground process cannot be read",
            )
        if command in _SHELLS:
            return _refuse("send_keys_target_bare_shell", "the target is a bare shell")
        if command not in _AGENT_CLIS:
            return _refuse(
                "send_keys_target_unconfirmed",
                f"the foreground process {command} is not an agent CLI",
            )

        composer = read_composer(terminal.id)
        if composer.state != "empty":
            return _refuse(
                "send_keys_composer_not_idle",
                f"the target composer is {composer.state}, not an idle agent composer",
            )

        pane = live_runtime_pane(session_id, terminal_manager, terminal_runtime_registry)
        snapshot = await pane.snapshot(DEFAULT_SNAPSHOT_LINES) if pane is not None else None
        if snapshot is None or not snapshot.strip():
            return _refuse(
                "send_keys_target_unconfirmed",
                "the target pane cannot be read or is blank",
            )
        if _CREDENTIAL_PROMPT.search(_last_nonblank_line(snapshot)):
            return _refuse("send_keys_credential_prompt", "the target shows a credential prompt")
    except Exception:
        # Fail closed: an unreadable target never receives keys.
        logger.warning("send_keys target check failed for %s", session_id, exc_info=True)
        return _refuse("send_keys_target_unconfirmed", "the target state could not be confirmed")
    return None
