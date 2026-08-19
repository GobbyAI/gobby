"""Neutral working directory for daemon one-shot text generation.

This is a low-level module: it must NOT import ``gobby.llm.claude`` or the
``gobby.ai`` text-generation adapters, because both of those import this helper.
Keeping it dependency-free avoids an ``llm.claude -> ai.adapters -> llm.claude``
import cycle.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

_NEUTRAL_TEXTGEN_CWD_PREFIX = "gobby-textgen-"

# Claude Code slugs a cwd path into a ~/.claude/projects dir name by replacing
# separators with '-', so per-call temp cwds appear there as
# '...-T-gobby-textgen---<suffix>'. The fixed cwd below deliberately avoids the
# trailing '-' marker so the purge can never match it.
_TEXTGEN_PROJECT_DIR_MARKER = "gobby-textgen-"
_FIXED_TEXTGEN_DIRNAME = "textgen"


def _claude_project_slug(path: Path) -> str:
    return str(path).replace("/", "-").replace(".", "-")


def _gobby_textgen_project_slug_fragment() -> str:
    return _claude_project_slug(Path(tempfile.gettempdir()) / _NEUTRAL_TEXTGEN_CWD_PREFIX)


@contextlib.contextmanager
def neutral_textgen_cwd() -> Iterator[Path]:
    """Yield a private temp directory to use as the cwd for one-shot text generation.

    Daemon-owned feature/text-gen CLI and SDK calls must NOT run in the project
    directory. Running there loads project context (``AGENTS.md``, project skills,
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


@contextlib.contextmanager
def fixed_textgen_cwd() -> Iterator[Path]:
    """Yield the stable daemon-owned cwd for one-shot Claude text generation.

    Claude Code materializes a directory under ``~/.claude/projects`` for every
    distinct cwd it runs in; per-call temp cwds created one throwaway project
    dir per generation (53k dirs / 2.9 GB before #20450). A fixed path keeps
    that to a single slug. Callers must NOT place per-call artifacts here —
    use :func:`neutral_textgen_cwd` when artifact isolation is needed.
    """
    cwd = Path.home() / ".gobby" / "tmp" / _FIXED_TEXTGEN_DIRNAME
    cwd.mkdir(parents=True, exist_ok=True)
    yield cwd


def purge_textgen_project_dirs(
    projects_root: Path | None = None,
    *,
    older_than_seconds: float = 3600.0,
    max_dirs: int = 1000,
) -> int:
    """Delete Claude project dirs left behind by per-call textgen cwds.

    Matches only slugs carrying the per-call ``gobby-textgen-`` marker; the
    fixed cwd's slug never matches. The age guard keeps an in-flight run from a
    still-running older daemon from losing its transcript mid-call.
    """
    root = projects_root if projects_root is not None else Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for entry in root.iterdir():
        if removed >= max_dirs:
            break
        if _gobby_textgen_project_slug_fragment() not in entry.name or not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(entry)
        except OSError:
            continue
        removed += 1
    return removed
