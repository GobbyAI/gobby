"""Validated `git apply` rewrites.

A literal `git apply <flags> <patch>` is classified from the patch bytes Gobby reads.
Rewriting it to read those same bytes from a quoted heredoc on stdin closes the window
in which another process could replace the patch file between the check and the apply.
Only a provider that applies rewritten input gets the rewrite; every other form stays an
unknown-scope write.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
from contextvars import ContextVar
from pathlib import Path

from gobby.hooks.events import SessionSource

# The hook reads the patch outside the seat sandbox and embeds it in the command, so the
# read is bounded: a larger patch stays an unknown-scope write.
MAX_PATCH_BYTES = 1024 * 1024
# Internal channel from the git classifier to the Bash normalizer. Never event data.
VALIDATED_APPLY_TEXT = "_validated_apply_text"
# The patch file the hook read, published so read rules see the read.
APPLY_READ_PATHS_KEY = "canonical_read_file_paths"
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


def read_validated_patch(root: Path, patch: Path) -> str | None:
    """The patch text when its bytes form one complete, strict UTF-8 patch, else None.

    The hook runs outside the seat sandbox and returns the bytes to the CLI, so it reads
    only a regular file inside the checkout root: no `..` component, and no symlink at
    any level below the root. Anything else is refused unread.
    """
    try:
        relative = patch.relative_to(root)
    except ValueError:
        return None
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    raw = _read_without_following(root, parts)
    if raw is None:
        return None
    if len(raw) > MAX_PATCH_BYTES or b"\x00" in raw or not raw.endswith(b"\n"):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_without_following(root: Path, parts: tuple[str, ...]) -> bytes | None:
    """Read root/parts through directory descriptors that never follow a symlink."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        return None
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY))
        for part in parts[:-1]:
            descriptors.append(
                os.open(part, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=descriptors[-1])
            )
        # Non-blocking so a FIFO cannot stall the hook; only a regular file is read. A
        # second hard link could name a file outside the root, so only a sole link is read.
        descriptors.append(
            os.open(parts[-1], os.O_RDONLY | nofollow | os.O_NONBLOCK, dir_fd=descriptors[-1])
        )
        status = os.fstat(descriptors[-1])
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            return None
        with os.fdopen(descriptors.pop(), "rb") as stream:
            return stream.read(MAX_PATCH_BYTES + 1)
    except OSError:
        return None
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


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
