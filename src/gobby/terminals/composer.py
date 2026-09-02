"""Composer clearing sequences and prompt-line inspection for Gobby injections.

Gobby types into a CLI's composer for handoff commands (``/clear``, ``/compact``),
wake messages, and the handoff pull prompt. An operator draft already in the box
would otherwise be submitted together with the injected text, so every injection
clears the composer first. Slash commands are additionally verified on the prompt
line before Enter is sent.
"""

from __future__ import annotations

from gobby.terminals.runtime import NamedKey

__all__ = [
    "COMPOSER_CAPTURE_LINES",
    "COMPOSER_DRAIN_LINES",
    "COMPOSER_DRAIN_MAX_ROUNDS",
    "COMPOSER_PROMPT_GLYPHS",
    "composer_clear_sequence",
    "composer_is_bare",
    "composer_line_is",
    "composer_prompt_line",
]

# Lines captured from the bottom of the pane when inspecting the composer.
COMPOSER_CAPTURE_LINES = 12
# Lines a non-Claude drain removes per pass; each pass empties the cursor line and
# joins its neighbours, so this bounds the multi-line draft one pass can clear.
COMPOSER_DRAIN_LINES = 8
# Verified drains stop after this many passes even if the capture keeps changing.
COMPOSER_DRAIN_MAX_ROUNDS = 6
# Prompt markers rendered by the supported CLIs (see install/shared/detection/*.toml).
COMPOSER_PROMPT_GLYPHS = "❯>$›"
_FRAME_GLYPHS = "│┃║"

# Claude Code binds ctrl+l to chat:clearInput, which empties the whole buffer.
_CLAUDE_CLEAR: tuple[NamedKey, ...] = ("ctrl_l",)
# Other composers only expose kill-to-line-start / kill-to-line-end, so the
# drain deletes the cursor line and joins its neighbours, repeatedly. Each key is
# a no-op on an empty buffer. Never escape or ctrl+c: those cancel turns.
_DRAIN_PASS: tuple[NamedKey, ...] = ("ctrl_u", "ctrl_k", "backspace", "delete")


def composer_clear_sequence(cli_source: str | None) -> tuple[NamedKey, ...]:
    """Keys that empty the composer for ``cli_source`` without cancelling a turn."""
    if cli_source == "claude":
        return _CLAUDE_CLEAR
    return _DRAIN_PASS * COMPOSER_DRAIN_LINES


def _strip_frame(line: str) -> str:
    return line.strip().strip(_FRAME_GLYPHS).strip()


def _is_prompt_line(line: str) -> bool:
    stripped = _strip_frame(line)
    return bool(stripped) and stripped[0] in COMPOSER_PROMPT_GLYPHS


def _prompt_content(line: str) -> str:
    return _strip_frame(line).lstrip(COMPOSER_PROMPT_GLYPHS).strip()


def composer_prompt_line(capture: str) -> str | None:
    """Return the bottom-most prompt line of a pane capture, or None."""
    for line in reversed(capture.splitlines()):
        if _is_prompt_line(line):
            return line
    return None


def composer_line_is(line: str | None, text: str) -> bool:
    """True when ``line`` is a prompt line holding exactly ``text``."""
    if line is None or not _is_prompt_line(line):
        return False
    return _prompt_content(line) == text.strip()


def composer_is_bare(capture: str) -> bool:
    """True when the bottom-most prompt line holds no draft text."""
    line = composer_prompt_line(capture)
    return line is not None and _prompt_content(line) == ""
