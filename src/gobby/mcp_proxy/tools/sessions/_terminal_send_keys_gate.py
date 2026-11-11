"""Target checks an allowed ``send_keys`` caller must pass before keys are written.

Configured rules decide who may call ``send_keys``. This module
decides whether the target can take keys: a live agent CLI in the foreground, an
empty composer that the composer ledger vouches for, and no credential prompt on
screen. Every signal that cannot be read refuses.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.idle_detector import COMPOSER_PROBE_LINES, plain_text
from gobby.agents.srt_runtime import srt_install_root
from gobby.sessions.terminal_kill import process_identity_matches
from gobby.terminals.composer_ledger import read_composer
from gobby.terminals.foreground import (
    command_name,
    foreground_executables,
    foreground_group,
    foreground_node_script,
    group_and_executable,
    node_script,
    shell_pid,
)
from gobby.terminals.pane_io import clear_composer, composer_reader, live_runtime_pane

logger = logging.getLogger(__name__)

SEND_MESSAGE_HINT = "Use gobby-agents:send_message for cross-session communication."

_SHELLS = frozenset({"ash", "bash", "csh", "dash", "fish", "ksh", "sh", "tcsh", "zsh"})
# The agent CLIs, as SessionSource names them and the daemon's PATH launches them. Any other
# foreground process, a shell or an editor or interpreter alike, is not a composer to type into.
_AGENT_CLIS = frozenset({"agy", "claude", "codex", "droid", "grok"})
# Versioned builds an installer keeps side by side in the directory its launcher resolves to.
_VERSIONED_BUILDS = {
    "claude": re.compile(r"\d+(?:\.\d+)+"),
    "grok": re.compile(r"grok-\d+(?:\.\d+)+-[a-z0-9_]+-[a-z0-9_]+"),
}
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


def _on_disk(path: str) -> Path | None:
    """``path`` resolved through its links, or None when it is relative or missing."""
    candidate = Path(path)
    if not candidate.is_absolute():
        return None
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _installed_cli(cli: str) -> Path | None:
    """The build the daemon's PATH launches for ``cli``, resolved through its links."""
    located = shutil.which(cli)
    return None if located is None else _on_disk(located)


@dataclass(frozen=True)
class _Seat:
    """The target session's recorded CLI process and its terminal's foreground group."""

    terminal_context: Mapping[str, Any]
    foreground_group: int

    def process(self) -> tuple[int, str] | None:
        """The session's recorded CLI process as ``(pid, executable)``, live in the foreground.

        Its pid and start time were recorded from the process that ran the session's own
        hooks, so a process that never started this session cannot match, whatever file
        it runs or names. The executable is the kernel's, never a path the process chose.
        """
        pid = self.terminal_context.get("parent_pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None
        if not process_identity_matches(pid, self.terminal_context):
            return None
        read = group_and_executable(pid)
        if read is None or read[0] != self.foreground_group:
            return None
        return pid, read[1]


def _agent_cli(executable: str) -> str | None:
    """The agent CLI a foreground executable is an installed build of, or None.

    A name proves nothing: any program can be renamed or planted as ``claude``. The
    executable counts when it resolves to the build the daemon's PATH resolves for that
    CLI. Claude's and Grok's installers keep each version beside the current one
    (``claude/versions/<version>``, ``.grok/downloads/grok-<version>-<os>-<arch>``) and a
    running session keeps its build across an update, so a version-named sibling in the
    installed build's own directory counts too. Planting one there grants nothing: whoever
    can write that directory can already replace the installed build itself.
    """
    running = _on_disk(executable)
    if running is None:
        return None
    for cli in sorted(_AGENT_CLIS):
        installed = _installed_cli(cli)
        if installed is None:
            continue
        if running == installed:
            return cli
        build = _VERSIONED_BUILDS.get(cli)
        if (
            build is not None
            and running.parent == installed.parent
            and build.fullmatch(running.name) is not None
        ):
            return cli
    return None


def _is_installed_srt_runner(script: str) -> bool:
    """Whether ``script`` is the SRT runner this install launches providers through."""
    try:
        installed = (srt_install_root() / "runner.mjs").resolve(strict=True)
        return Path(script).resolve(strict=True) == installed
    except (OSError, RuntimeError):
        return False


def _node_agent_cli(pid: int, seat: _Seat | None) -> str | None:
    """Bind a foreground node to the agent CLI it runs, through the session's own process.

    node's script is a path read again at check time, so it only names the CLI: a link
    node opened while it named another file, then pointed at the installed one, reads the
    same. What admits is the session's recorded CLI process, live in the foreground. For
    Gobby's installed SRT runner that is the provider it launched, an installed agent CLI
    build, or node on Claude's npm script. For the script the daemon's PATH resolves
    ``claude`` to (an npm install) it is node itself, leading the foreground group. A
    package's own metadata proves nothing, since any directory can declare itself Claude's
    package.
    """
    script = foreground_node_script(pid)
    live = None if seat is None else seat.process()
    if script is None or seat is None or live is None:
        return None
    if _is_installed_srt_runner(script):
        cli = _agent_cli(live[1])
        if cli is None and command_name(live[1]) == "node" and _is_npm_claude(node_script(live[0])):
            return "claude"
        return cli
    if not _is_npm_claude(script):
        return None
    return "claude" if live[0] == seat.foreground_group else None


def _is_npm_claude(script: str | None) -> bool:
    """Whether node's ``script`` is the entrypoint the daemon's PATH resolves ``claude`` to."""
    entrypoint = None if script is None else _on_disk(script)
    return entrypoint is not None and entrypoint == _installed_cli("claude")


def _detection_registry(terminal_manager: Any) -> DetectionManifestRegistry:
    """The installed provider composer manifests, read from the hub database."""
    return DetectionManifestRegistry(terminal_manager.db)


def _seat(pid: int | None, terminal_context: Mapping[str, Any] | None) -> _Seat | None:
    """The target session's recorded CLI process, when the session and its shell record one."""
    if pid is None or terminal_context is None:
        return None
    group = foreground_group(pid)
    return None if group is None else _Seat(terminal_context, group)


def _classify(executable: str, pid: int | None, seat: _Seat | None) -> str | None:
    cli = _agent_cli(executable)
    if cli is None and command_name(executable) == "node" and pid is not None:
        cli = _node_agent_cli(pid, seat)
    return cli


async def _foreground_agent_cli(
    terminal: Any, terminal_context: Mapping[str, Any] | None
) -> SendKeysRefusal | str:
    """The agent CLI in the terminal's foreground, or why the process is no agent CLI.

    Process inspection only: nothing here reads what the CLI drew on screen.
    ``terminal_context`` is the target session's recorded process identity.
    """
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
    seat = await asyncio.to_thread(_seat, pid, terminal_context)
    cli = await asyncio.to_thread(_classify, executable, pid, seat)
    if cli is None:
        return _refuse(
            "send_keys_target_unconfirmed",
            f"the foreground process {command} is not an agent CLI",
        )
    return cli


async def wake_target_refusal(
    session_id: str, terminal: Any, terminal_context: Mapping[str, Any] | None
) -> SendKeysRefusal | None:
    """Return why a live wake must not type into ``terminal``, or ``None``.

    A wake reads no pane text: whether the seat may be typed into is decided by its
    session row, pane focus and the keystroke composer ledger before this check. Here
    only the foreground process is confirmed to be an agent CLI, so a wake never types
    into a bare shell, an editor or a ``sudo`` prompt.
    """
    try:
        found = await _foreground_agent_cli(terminal, terminal_context)
    except Exception:
        # Fail closed: an unreadable target never receives keys.
        logger.warning("wake target check failed for %s", session_id, exc_info=True)
        return _refuse("send_keys_target_unconfirmed", "the target state could not be confirmed")
    return found if isinstance(found, SendKeysRefusal) else None


async def send_keys_target_refusal(
    session_id: str,
    terminal_manager: Any | None,
    terminal_runtime_registry: Any | None,
    *,
    terminal_context: Mapping[str, Any] | None,
    clear_held: bool = False,
) -> SendKeysRefusal | ComposerCleared | None:
    """Return why the target cannot take keys, or ``None`` when it shows an idle agent composer.

    ``clear_held`` is set for a pure clear-key request. When the ledger holds daemon
    text, the composer is drained through the pane (recording the drain, so no held
    command is replayed later) and ``ComposerCleared`` is returned; every other
    check still applies.
    """
    if terminal_manager is None or terminal_runtime_registry is None:
        return _refuse("send_keys_target_unconfirmed", "the target terminal cannot be inspected")
    try:
        terminal = terminal_manager.get_live_for_session(session_id)
        if terminal is None:
            return _refuse("send_keys_no_live_terminal", "the target has no live terminal")

        found = await _foreground_agent_cli(terminal, terminal_context)
        if isinstance(found, SendKeysRefusal):
            return found
        cli = found

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
        if manifest is None or read_frame is None:
            return _refuse(
                "send_keys_target_unconfirmed",
                f"no composer probe is installed for the {cli} agent",
            )
        pane = live_runtime_pane(session_id, terminal_manager, terminal_runtime_registry, terminal)
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
