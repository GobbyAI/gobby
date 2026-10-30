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
from pathlib import Path
from typing import Any

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.idle_detector import COMPOSER_PROBE_LINES, plain_text
from gobby.terminals.composer_ledger import read_composer
from gobby.terminals.foreground import command_name, foreground_executables, shell_pid
from gobby.terminals.pane_io import composer_reader, live_runtime_pane

logger = logging.getLogger(__name__)

SEND_MESSAGE_HINT = "Use gobby-agents:send_message for cross-session communication."

_SHELLS = frozenset({"ash", "bash", "csh", "dash", "fish", "ksh", "sh", "tcsh", "zsh"})
# Bare command names of the agent CLIs, as SessionSource names them. Any other foreground
# process, a shell or an editor or interpreter alike, is not a composer to type into.
_AGENT_CLIS = frozenset({"agy", "claude", "codex", "droid", "grok"})
_CLAUDE_VERSION = re.compile(r"\d+(?:\.\d+)+")
_CREDENTIAL_PROMPT = re.compile(
    r"\b(password|passphrase|passcode|pin|api[ _-]?key|token|secret|otp)\b[^\n]*:\s*$",
    re.IGNORECASE,
)
# Live-turn rules. A first-match verdict ranks an idle hook-denial line above them, so
# each one is checked by name and vetoes on its own, whatever its priority.
_BUSY_RULE_IDS = ("turn_in_flight", "active_work")


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


def _agent_cli(command: str, executable: str) -> str | None:
    """The agent CLI a foreground process runs, or None when it runs none.

    A bare name in the allowlist is the CLI itself. The native installer runs a Claude
    build from ``claude/versions/<version>``, so its command name is a version string;
    that path counts only when it resolves on disk to such a build.
    """
    if command in _AGENT_CLIS:
        return command
    return "claude" if _is_versioned_claude(executable) else None


def _is_versioned_claude(executable: str) -> bool:
    path = Path(executable)
    if not path.is_absolute():
        return False
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return (
        resolved.parent.name == "versions"
        and resolved.parent.parent.name == "claude"
        and _CLAUDE_VERSION.fullmatch(resolved.name) is not None
    )


def _detection_registry(terminal_manager: Any) -> DetectionManifestRegistry:
    """The installed provider composer manifests, read from the hub database."""
    return DetectionManifestRegistry(terminal_manager.db)


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
        executables = {terminal.id: pid} if pid else {}
        foreground = await asyncio.to_thread(foreground_executables, executables)
        executable = foreground.get(terminal.id)
        if executable is None:
            return _refuse(
                "send_keys_target_unconfirmed",
                "the target's foreground process cannot be read",
            )
        command = command_name(executable)
        if command in _SHELLS:
            return _refuse("send_keys_target_bare_shell", "the target is a bare shell")
        cli = _agent_cli(command, executable)
        if cli is None:
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

        registry = _detection_registry(terminal_manager)
        read_frame = composer_reader(registry, cli)
        manifest = registry.for_provider(cli)
        if read_frame is None or manifest is None:
            return _refuse(
                "send_keys_target_unconfirmed",
                f"no composer probe is installed for the {cli} agent",
            )
        pane = live_runtime_pane(session_id, terminal_manager, terminal_runtime_registry)
        snapshot = (
            await pane.snapshot(COMPOSER_PROBE_LINES, mode="ansi") if pane is not None else None
        )
        text = plain_text(snapshot) if snapshot is not None else ""
        if not text.strip():
            return _refuse(
                "send_keys_target_unconfirmed",
                "the target pane cannot be read or is blank",
            )
        if _CREDENTIAL_PROMPT.search(_last_nonblank_line(text)):
            return _refuse("send_keys_credential_prompt", "the target shows a credential prompt")
        # Composer frames can remain visible during a turn. Inspect the entire
        # pane too, so provider activity or blocking prompts override that frame.
        # Status bars include the frame rules and do not establish activity.
        activity = manifest.match(text)
        # A busy rule whose pattern times out reports an issue, not a match, and that
        # uncertainty must refuse as well.
        busy_checks = [manifest.match_rule(rule_id, text) for rule_id in _BUSY_RULE_IDS]
        busy = any(check.match is not None or check.issues for check in busy_checks)
        if (
            activity.issues
            or activity.match is None
            or busy
            or (activity.match.state != "idle" and activity.match.rule_id != "status_bar")
            or read_frame(snapshot).state != "empty"
        ):
            return _refuse(
                "send_keys_composer_not_idle",
                "the target pane does not show an idle agent composer",
            )
    except Exception:
        # Fail closed: an unreadable target never receives keys.
        logger.warning("send_keys target check failed for %s", session_id, exc_info=True)
        return _refuse("send_keys_target_unconfirmed", "the target state could not be confirmed")
    return None
