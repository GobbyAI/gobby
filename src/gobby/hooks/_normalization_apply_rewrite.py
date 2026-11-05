"""Validated `git apply` rewrites.

A literal `git apply <flags> <patch>` is classified from the patch bytes Gobby reads.
Rewriting it to read those same bytes from a quoted heredoc on stdin closes the window
in which another process could replace the patch file between the check and the apply.
Only a provider that applies rewritten input gets the rewrite; every other form stays an
unknown-scope write.
"""

from __future__ import annotations

import re
import secrets
from contextvars import ContextVar
from pathlib import Path

from gobby.hooks.events import SessionSource

MAX_PATCH_BYTES = 16 * 1024 * 1024
# Internal channel from the git classifier to the Bash normalizer. Never event data.
VALIDATED_APPLY_TEXT = "_validated_apply_text"
# The rewritten command rule evaluation reads. Underscored like `_raw_tool_input`.
APPLY_REWRITE_KEY = "_validated_apply_command"
# Set only while normalizing a command that is exactly one literal `git apply`.
apply_rewrite_scope: ContextVar[bool] = ContextVar("apply_rewrite_scope", default=False)

_SIMPLE_WORD = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")


def applies_rewritten_input(source: object) -> bool:
    """Whether the provider runs the rewritten tool input a rule returns.

    A provider that ignores the rewrite would run the command the CLI sent after its
    patch had been read, so that provider keeps the unknown-scope classification.
    """
    return source == SessionSource.CLAUDE


def simple_apply_words(command: str) -> list[str] | None:
    """The words of a command that is exactly one literal `git apply`, or None.

    Words are separated by single spaces. Quoting, expansion, operators, redirection,
    the option terminator and a stdin operand all fall outside the form.
    """
    words = command.split(" ")
    if len(words) < 3 or words[:2] != ["git", "apply"] or "--" in words:
        return None
    if words[-1].startswith("-") or not all(_SIMPLE_WORD.fullmatch(word) for word in words):
        return None
    return words


def read_validated_patch(patch: Path) -> str | None:
    """The patch text when its bytes form one complete, strict UTF-8 patch, else None."""
    try:
        if not patch.is_file():
            return None
        with patch.open("rb") as stream:
            raw = stream.read(MAX_PATCH_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_PATCH_BYTES or b"\x00" in raw or not raw.endswith(b"\n"):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def new_delimiter() -> str:
    return secrets.token_hex(8)


def build_apply_rewrite(words: list[str], text: str) -> str | None:
    """`git apply <flags> - <<'DELIM'` with the validated text as its heredoc body.

    A quoted delimiter disables expansion inside the body. None when a body line would
    end the heredoc early.
    """
    delimiter = new_delimiter()
    if any(line == delimiter for line in text.split("\n")):
        return None
    head = " ".join([*words[:-1], "-", f"<<'{delimiter}'"])
    return f"{head}\n{text}{delimiter}\n"
