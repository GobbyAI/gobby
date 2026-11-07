"""Target checks an allowed ``send_keys`` caller must pass before keys are written.

Configured rules decide who may call ``send_keys``. This module
decides whether the target can take keys: a live agent CLI in the foreground, an
empty composer that the composer ledger vouches for, and no credential prompt on
screen. Every signal that cannot be read refuses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.idle_detector import COMPOSER_PROBE_LINES, plain_text
from gobby.agents.srt_runtime import srt_install_root
from gobby.terminals.composer_ledger import read_composer
from gobby.terminals.foreground import (
    command_name,
    foreground_child_executables,
    foreground_executables,
    foreground_node_script,
    shell_pid,
)
from gobby.terminals.pane_io import clear_composer, composer_reader, live_runtime_pane

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


_HELD_RECOVERY = (
    "The composer holds unsubmitted daemon text, such as a staged /compact. Clear all of it "
    "with one clear key, such as send_keys(keys='ctrl_u', literal=false), or rerun "
    "set_handoff to resubmit it."
)
_DRAFT_RECOVERY = (
    "Keys never clear an operator draft or an unconfirmed composer. Once the pane shows "
    "no draft, free it from another session with gobby-sessions:release_composer."
)


@dataclass(frozen=True)
class SendKeysRefusal:
    code: str
    message: str
    recovery: str | None = None


@dataclass(frozen=True)
class ComposerCleared:
    """A held daemon draft was drained through the pane instead of taking the keys."""


def _refuse(code: str, reason: str, recovery: str | None = None) -> SendKeysRefusal:
    return SendKeysRefusal(code, f"send_keys refused: {reason}. {SEND_MESSAGE_HINT}", recovery)


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


def _is_installed_srt_runner(script: str) -> bool:
    """Whether ``script`` is the SRT runner this install launches providers through."""
    try:
        installed = (srt_install_root() / "runner.mjs").resolve(strict=True)
        return Path(script).resolve(strict=True) == installed
    except (OSError, RuntimeError):
        return False


def _srt_runner_cli(pid: int) -> str | None:
    """The agent CLI an SRT runner launched, read from its live child in the same group.

    The runner's script names no provider; its child does. The child is classified like
    any foreground executable, so only an allowlisted CLI name or a versioned Claude
    build on disk qualifies.
    """
    for executable in foreground_child_executables(pid):
        cli = _agent_cli(command_name(executable), executable)
        if cli is not None:
            return cli
    return None


def _node_agent_cli(pid: int) -> str | None:
    """Bind node's live script to the agent CLI it runs.

    Either the installed Claude package's declared CLI, or Gobby's installed SRT runner
    with its provider as a live child.
    """
    script = foreground_node_script(pid)
    if script is None:
        return None
    if _is_installed_srt_runner(script):
        return _srt_runner_cli(pid)
    try:
        entrypoint = Path(script).resolve(strict=True)
        manifest_path = entrypoint.parent / "package.json"
        if not entrypoint.is_file() or manifest_path.stat().st_size > 65536:
            return None
        package = json.loads(manifest_path.read_text())
        if not isinstance(package, dict) or package.get("name") != "@anthropic-ai/claude-code":
            return None
        bins = package.get("bin")
        relative = bins.get("claude") if isinstance(bins, dict) else None
        if not isinstance(relative, str):
            return None
        if (entrypoint.parent / relative).resolve(strict=True) == entrypoint:
            return "claude"
    except (OSError, ValueError):
        return None
    return None


def _detection_registry(terminal_manager: Any) -> DetectionManifestRegistry:
    """The installed provider composer manifests, read from the hub database."""
    return DetectionManifestRegistry(terminal_manager.db)


async def send_keys_target_refusal(
    session_id: str,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
    *,
    clear_held: bool = False,
    resolved_terminal: Any | None = None,
    ledger_vouches: bool = False,
) -> SendKeysRefusal | ComposerCleared | None:
    """Return why the target cannot take keys, or ``None`` when it shows an idle agent composer.

    ``clear_held`` is set for a pure clear-key request. When the ledger holds daemon
    text, the composer is drained through the pane (recording the drain, so no held
    command is replayed later) and ``ComposerCleared`` is returned; every other
    check still applies.

    ``ledger_vouches`` is set by a live wake. A CLI whose manifest has no composer rules
    (Grok) has no frame to read, so the ledger's empty verdict confirms its composer; the
    identity, credential, busy and idle-pane checks still apply. Without it, such a CLI is
    refused, as send_keys has always refused it.
    """
    if terminal_manager is None or terminal_runtime_registry is None:
        return _refuse("send_keys_target_unconfirmed", "the target terminal cannot be inspected")
    try:
        # A wake passes the terminal it resolved, which may be bound by context id
        # rather than by session id; the session lookup is the send_keys fallback.
        terminal = (
            resolved_terminal
            if resolved_terminal is not None
            else terminal_manager.get_live_for_session(session_id)
        )
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
        if cli is None and command == "node" and pid is not None:
            cli = await asyncio.to_thread(_node_agent_cli, pid)
        if cli is None:
            return _refuse(
                "send_keys_target_unconfirmed",
                f"the foreground process {command} is not an agent CLI",
            )

        composer = read_composer(terminal.id)
        draining = clear_held and composer.state == "held"
        if composer.state != "empty" and not draining:
            return _refuse(
                "send_keys_composer_not_idle",
                f"the target composer is {composer.state}, not an idle agent composer",
                _HELD_RECOVERY if composer.state == "held" else _DRAFT_RECOVERY,
            )

        registry = _detection_registry(terminal_manager)
        read_frame = composer_reader(registry, cli)
        manifest = registry.for_provider(cli)
        if manifest is None or (read_frame is None and not ledger_vouches):
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
            # A draft visible in the frame is touched by the drain alone.
            or (not draining and read_frame is not None and read_frame(snapshot).state != "empty")
        ):
            return _refuse(
                "send_keys_composer_not_idle",
                "the target pane does not show an idle agent composer",
            )
        if draining:
            if pane is None:
                return _refuse("send_keys_target_unconfirmed", "the target pane cannot be read")
            drained = await clear_composer(pane, cli, read_frame)
            if not drained.ok:
                return _refuse(
                    "send_keys_composer_not_idle",
                    f"the held composer could not be cleared: {drained.reason}",
                    _HELD_RECOVERY,
                )
            return ComposerCleared()
    except Exception:
        # Fail closed: an unreadable target never receives keys.
        logger.warning("send_keys target check failed for %s", session_id, exc_info=True)
        return _refuse("send_keys_target_unconfirmed", "the target state could not be confirmed")
    return None
