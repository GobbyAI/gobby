"""Whether a green validation run covers a red one, for the found-work stop gate."""

from __future__ import annotations

import os.path
import re
import shlex
from collections.abc import Sequence
from pathlib import Path

from gobby.tasks.command_equivalence import target_covers
from gobby.tasks.transcript_evidence_models import (
    TranscriptValidationRun,
    TranscriptValidationSegment,
)

_UV_DIRECTORY_OPTIONS = frozenset({"--directory", "--project"})
_INI_OVERRIDE_OPTIONS = frozenset({"-o", "--override-ini"})
_INI_OVERRIDE_PREFIXES = ("--override-ini=", "-opythonpath=")
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|"})

_SCOPE_OPTION_NAMES = frozenset({"-k", "-m", "--filter", "--run", "-run"})
# ``-p`` selects a crate for cargo; for pytest it loads a plugin (``-p no:cacheprovider``).
_CARGO_SCOPE_OPTION_NAMES = _SCOPE_OPTION_NAMES | {"-p", "--package"}
_PYTHON_LAUNCHER_RE = re.compile(r"^python(?:\d+(?:\.\d+)?)?$")
_COVER_SUFFIXES = {
    ".py",
    ".pyi",
    ".rs",
    ".ts",
    ".tsx",
    ".js",
    ".mjs",
    ".cjs",
    ".go",
}
_COVER_ROOTS = ("tests/", "src/", "crates/")


def run_covers(success: TranscriptValidationRun, failure: TranscriptValidationRun) -> bool:
    """Return whether every red validation segment sits inside a green segment.

    Segments compare one to one, never as a union across the run: a lint
    segment's paths cannot cover a test segment, and a sibling segment's
    selectors cannot narrow it.
    """
    greens = _run_segments(success)
    return all(
        any(_segment_covers(green, red) for green in greens) for red in _run_segments(failure)
    )


def _segment_covers(
    success: TranscriptValidationSegment,
    failure: TranscriptValidationSegment,
) -> bool:
    """Same category, no extra selectors (``-k``, ``-m``, ...), and containing paths."""
    if not set(success.categories) & set(failure.categories):
        return False
    success_paths, success_selectors = _split_targets(_command_targets(success.command))
    failure_paths, failure_selectors = _split_targets(_command_targets(failure.command))
    if not success_selectors <= failure_selectors:
        return False
    if not success_paths:
        return True
    if not failure_paths:
        return False
    return all(any(target_covers(green, red) for green in success_paths) for red in failure_paths)


def _split_targets(targets: Sequence[str]) -> tuple[tuple[str, ...], frozenset[str]]:
    paths = tuple(target for target in targets if not target.startswith("-"))
    selectors = frozenset(target for target in targets if target.startswith("-"))
    return paths, selectors


def _run_segments(run: TranscriptValidationRun) -> tuple[TranscriptValidationSegment, ...]:
    """The run's validation segments; the whole command when it was built unclassified."""
    return run.validation_segments or (
        TranscriptValidationSegment(command=run.command, categories=run.categories),
    )


def run_targets(run: TranscriptValidationRun) -> tuple[str, ...]:
    """Cover targets across every validation segment, for the foreign-path checks."""
    return tuple(
        dict.fromkeys(
            target for segment in _run_segments(run) for target in _command_targets(segment.command)
        )
    )


def _command_targets(command: str) -> tuple[str, ...]:
    try:
        tokens = _drop_python_launcher(shlex.split(command))
    except ValueError:
        return ()
    program = next((token for token in tokens if "=" not in token), "")
    scope_options = _CARGO_SCOPE_OPTION_NAMES if program == "cargo" else _SCOPE_OPTION_NAMES
    targets: list[str] = []
    for index, token in enumerate(tokens):
        if token in scope_options and index + 1 < len(tokens):
            value = tokens[index + 1]
            if "/" in value or Path(value).suffix:
                normalized_value = value.removeprefix("./").rstrip("/")
                if _is_cover_target(normalized_value):
                    targets.append(normalized_value)
                continue
            targets.append(f"{token}:{value}")
            continue
        if token.startswith("-") or "=" in token or token in {"&&", "||", ";"}:
            continue
        normalized = token.removeprefix("./").rstrip("/")
        if _is_cover_target(normalized):
            targets.append(normalized)
    return tuple(dict.fromkeys(targets))


def _drop_python_launcher(tokens: list[str]) -> list[str]:
    """Drop ``python -m <module>`` launcher triples; that ``-m`` is no marker selector."""
    kept: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            _PYTHON_LAUNCHER_RE.match(token)
            and index + 2 < len(tokens)
            and tokens[index + 1] == "-m"
        ):
            index += 3
            continue
        kept.append(token)
        index += 1
    return kept


def _is_cover_target(token: str) -> bool:
    """Keep source/test paths; drop log redirects and mkdir/cd directories."""
    path = token.split("::", 1)[0]
    suffix = Path(path).suffix.lower()
    if suffix in _COVER_SUFFIXES:
        return True
    normalized = path.replace("\\", "/")
    return any(
        normalized == root.rstrip("/")
        or normalized.startswith(root)
        or f"/{root}" in f"/{normalized}/"
        for root in _COVER_ROOTS
    )


def green_covers_failure(
    green: TranscriptValidationRun,
    failure: TranscriptValidationRun,
    project_path: str | None,
) -> bool:
    """A later green covers as before; an earlier one only from another source tree.

    A green of the same selectors on a different tree confines the red to its
    own tree in either order, as in a base reproduction beside a candidate run.
    On one tree, a green followed by a red is a regression the red still shows.
    """
    if not run_covers(green, failure):
        return False
    return green.order > failure.order or _source_tree(green.command, project_path) != (
        _source_tree(failure.command, project_path)
    )


def _source_tree(command: str, base: str | None) -> tuple[str | None, str | None]:
    """Return the ``(directory, pythonpath)`` a validation command runs against.

    The directory starts at ``base`` and follows ``cd`` and uv's ``--directory``
    or ``--project``, before or after ``run``. The pythonpath comes from a
    ``PYTHONPATH=`` assignment or pytest's ``-o pythonpath=``. Relative values
    resolve against the directory in effect when they appear.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return base, None
    directory, pythonpath = base, None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        option, _, inline = token.partition("=")
        if token == "cd" and following is not None and _starts_segment(tokens, index):
            directory = _resolve(directory, following)
        elif option in _UV_DIRECTORY_OPTIONS and (inline or following):
            directory = _resolve(directory, inline or following or "")
        elif option == "PYTHONPATH" and inline:
            pythonpath = _resolve(directory, inline)
        elif token in _INI_OVERRIDE_OPTIONS and following is not None:
            pythonpath = _ini_pythonpath(following, directory) or pythonpath
        elif token.startswith(_INI_OVERRIDE_PREFIXES):
            value = token.removeprefix("--override-ini=").removeprefix("-o")
            pythonpath = _ini_pythonpath(value, directory) or pythonpath
        index += 1
    return directory, pythonpath


def _starts_segment(tokens: Sequence[str], index: int) -> bool:
    return index == 0 or tokens[index - 1] in _SHELL_SEPARATORS


def _ini_pythonpath(override: str, directory: str | None) -> str | None:
    key, _, value = override.partition("=")
    return _resolve(directory, value) if key.strip() == "pythonpath" and value else None


def _resolve(directory: str | None, value: str) -> str:
    path = os.path.expanduser(value)
    if directory is not None:
        path = os.path.join(directory, path)
    return os.path.normpath(path)
