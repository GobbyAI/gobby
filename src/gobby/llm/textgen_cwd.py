"""Neutral working directory for daemon one-shot text generation.

This is a low-level module: it must NOT import ``gobby.llm.claude`` or the
``gobby.ai`` text-generation adapters, because both of those import this helper.
Keeping it dependency-free avoids an ``llm.claude -> ai.adapters -> llm.claude``
import cycle.
"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Iterator
from pathlib import Path

_NEUTRAL_TEXTGEN_CWD_PREFIX = "gobby-textgen-"


@contextlib.contextmanager
def neutral_textgen_cwd() -> Iterator[Path]:
    """Yield a private temp directory to use as the cwd for one-shot text generation.

    Daemon-owned feature/text-gen CLI and SDK calls must NOT run in the project
    directory. Running there loads project context (``GEMINI.md``, project skills,
    gobby lifecycle hooks, configured MCP servers) and adds a variable 20-40s
    startup tax that pushes calls past the candidate timeout. One-shot generation
    needs no project context — the prompt carries everything.

    The directory lives for the whole ``with`` block, so callers may place
    per-call artifacts inside it (e.g. Codex ``--output-last-message`` files, Grok
    leader sockets, Droid home/state) and have them share the same lifetime as the
    spawned subprocess or SDK query.
    """
    with tempfile.TemporaryDirectory(prefix=_NEUTRAL_TEXTGEN_CWD_PREFIX) as temp_dir:
        yield Path(temp_dir)
