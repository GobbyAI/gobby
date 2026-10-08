"""Whether a green validation run covers a red one, for the found-work stop gate."""

from __future__ import annotations

import os.path
import re
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from gobby.hooks._path_scope import _is_temp_agent_scratchpad_path
from gobby.tasks.command_equivalence import canonical_command, target_covers
from gobby.tasks.transcript_evidence_models import (
    TranscriptValidationRun,
    TranscriptValidationSegment,
)
from gobby.utils import spawn

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
_NO_TESTS_RE = re.compile(r"^=+ no tests (?:collected|ran) in ", re.MULTILINE)
# Any sign that a test was collected or ran; such a red keeps full coverage.
_COLLECTED_RE = re.compile(
    r"\bcollected [1-9]|\[[1-9]\d* items?\]|^\s*(?:FAILED|PASSED)\b"
    r"|\b[1-9]\d* (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|selected)\b",
    re.MULTILINE,
)
_ERROR_LINE_RE = re.compile(r"^\s*ERROR\b.*$", re.MULTILINE)
_MISSING_PATH_RE = re.compile(r"ERROR: file or directory not found: (\S+)")
_CARGO_MISSING_PACKAGE_RE = re.compile(
    r"error: package ID specification `([^`]+)` did not match any packages"
)
_CARGO_NEXTEST_WRAPPER_RE = re.compile(r"error: command `([^`]+)` exited with code 101")
_CARGO_EXECUTION_RE = re.compile(
    r"^\s*(?:Compiling|Checking|Finished|Running)\b|\brunning \d+ tests?\b|test result:"
    r"|^\s*(?:PASS|FAIL|Summary)\b",
    re.MULTILINE,
)


def cargo_package_selection_failed(failure: TranscriptValidationRun) -> bool:
    """Recognize a Cargo package-selection error that never compiled or ran tests.

    Recorded output can include provider metadata and nextest's Cargo subprocess
    diagnostic. Require a complete missing-package diagnostic, reject execution
    markers and other errors, and verify any nextest wrapper selected that package.
    """
    if (
        failure.exit_code != 101
        or failure.output_truncated
        or not failure.output
        or failure.wrapped
        or "test" not in failure.categories
    ):
        return False
    command = canonical_command(failure.command)
    if command is None:
        return False
    tokens = shlex.split(command)
    if "&&" in tokens:
        return False
    if tokens[:2] == ["uv", "run"]:
        tokens = tokens[2:]
    if tokens[:2] != ["cargo", "test"] and tokens[:3] != ["cargo", "nextest", "run"]:
        return False
    args = tokens[: tokens.index("--")] if "--" in tokens else tokens
    packages = {args[index + 1] for index, token in enumerate(args[:-1]) if token == "--package"}
    lines = [line.strip() for line in failure.output.splitlines() if line.strip()]
    matches = [match for line in lines if (match := _CARGO_MISSING_PACKAGE_RE.fullmatch(line))]
    if (
        len(matches) != 1
        or matches[0].group(1) not in packages
        or _CARGO_EXECUTION_RE.search(failure.output)
        or _COLLECTED_RE.search(failure.output)
    ):
        return False
    package = matches[0].group(1)
    for line in lines:
        if not line.startswith("error:") or _CARGO_MISSING_PACKAGE_RE.fullmatch(line):
            continue
        wrapper = _CARGO_NEXTEST_WRAPPER_RE.fullmatch(line)
        if tokens[:3] != ["cargo", "nextest", "run"] or wrapper is None:
            return False
        wrapper_command = canonical_command(wrapper.group(1))
        if wrapper_command is None:
            return False
        wrapped = shlex.split(wrapper_command)
        if (
            len(wrapped) < 3
            or Path(wrapped[0]).name != "cargo"
            or wrapped[1] != "test"
            or "&&" in wrapped
        ):
            return False
        wrapped_args = wrapped[: wrapped.index("--")] if "--" in wrapped else wrapped
        if "--no-run" not in wrapped_args or not any(
            wrapped_args[index : index + 2] == ["--package", package]
            for index in range(len(wrapped_args) - 1)
        ):
            return False
    return True


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


# A long session's cover check compares every failure with every green; parse each
# command once rather than once per pair.
@lru_cache(maxsize=4096)
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


def surviving_path_failure(
    failure: TranscriptValidationRun,
    project_path: str | None,
) -> TranscriptValidationRun | None:
    """Narrow a pytest run that collected nothing because named paths are gone.

    Pytest stops with ``file or directory not found`` before collecting when a
    path argument does not exist. When the output shows no collected test (its
    summary, or usage-error exit 4 where compaction dropped the summary), that
    is the run's only error, and each reported path is one of its targets and
    is absent from both its source tree's working copy and HEAD, the red says
    nothing about the surviving targets, so it narrows to them. HEAD absence
    keeps an uncommitted ``rm`` of a failing test from clearing the gate.
    Anything else keeps the run as it was.
    """
    output = failure.output
    if (
        not output
        or failure.output_truncated
        or not (failure.exit_code == 4 or _NO_TESTS_RE.search(output))
        or _COLLECTED_RE.search(output)
    ):
        return None
    missing: set[str] = set()
    for line in _ERROR_LINE_RE.findall(output):
        match = _MISSING_PATH_RE.fullmatch(line.strip())
        if match is None:
            return None
        missing.add(match.group(1).removeprefix("./").rstrip("/"))
    directory = _source_tree(failure.command, project_path)[0]
    if not missing or directory is None or not missing <= set(run_targets(failure)):
        return None
    files = [path.split("::", 1)[0] for path in missing]
    if any(os.path.lexists(os.path.join(directory, path)) for path in files):
        return None
    if _tracked_at_head(directory, files, project_path):
        return None
    segments = tuple(
        TranscriptValidationSegment(
            command=_drop_targets(segment.command, missing), categories=segment.categories
        )
        for segment in _run_segments(failure)
    )
    if not any(_command_targets(segment.command) for segment in segments):
        return None
    return replace(
        failure,
        command=_drop_targets(failure.command, missing),
        validation_segments=segments if failure.validation_segments else (),
    )


def ran_in_scratchpad(run: TranscriptValidationRun, project_path: str | None) -> bool:
    """Whether a run exercised an agent scratchpad tree outside the session's checkout.

    The tree is the run's path targets, or its directory when it names none,
    plus every pythonpath entry. Such a red, as in a reviewer's base extract,
    is a deliberate probe, so it is no terminal failure of the session's own
    validation. Scratchpad tests run with the checkout's source on an explicit
    PYTHONPATH still count, and a checkout that itself lies in a scratchpad
    still owns its runs.
    """
    directory, pythonpath = _source_tree(run.command, project_path)
    if directory is None:
        return False
    paths = [
        os.path.join(directory, target.split("::", 1)[0])
        for target in run_targets(run)
        if not target.startswith("-")
    ] or [directory]
    paths.extend(pythonpath)
    checkout = Path(project_path).resolve() if project_path else None
    return all(
        _is_temp_agent_scratchpad_path(path)
        and (checkout is None or not path.is_relative_to(checkout))
        for path in (Path(raw).resolve() for raw in paths)
    )


def _tracked_at_head(directory: str, paths: Sequence[str], project_path: str | None) -> bool:
    """Whether HEAD tracks any of ``paths``; an unanswered lookup counts as tracked.

    A tree that is not a repository, such as a ``git archive`` export, cannot answer,
    so ``project_path``'s HEAD does.
    """
    for cwd in dict.fromkeys(filter(None, (directory, project_path))):
        try:
            result = spawn.run(
                ["git", "ls-tree", "--name-only", "HEAD", "--", *paths],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return bool(result.stdout.strip())
    return True


def _drop_targets(command: str, targets: set[str]) -> str:
    """Drop ``targets`` from ``command``; an unparsable command keeps them all."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    return shlex.join(
        token for token in tokens if token.removeprefix("./").rstrip("/") not in targets
    )


@lru_cache(maxsize=4096)
def _source_tree(command: str, base: str | None) -> tuple[str | None, tuple[str, ...]]:
    """Return the ``(directory, pythonpath entries)`` a validation command runs against.

    The directory starts at ``base`` and follows ``cd`` and uv's ``--directory``
    or ``--project``, before or after ``run``. ``--directory`` sets uv's working
    directory, so it outranks ``--project`` in either order, as in
    ``run_location``. The pythonpath entries come from a ``PYTHONPATH=``
    assignment, split on ``os.pathsep`` with an empty entry meaning the
    directory, or pytest's whitespace-separated ``-o pythonpath=``. Relative
    values resolve against the directory in effect when they appear.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return base, ()
    directory: str | None = base
    pythonpath: tuple[str, ...] = ()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        option, _, inline = token.partition("=")
        if token == "cd" and following is not None and _starts_segment(tokens, index):
            directory = _resolve(directory, following)
        elif option in _UV_DIRECTORY_OPTIONS and (inline or following):
            if option == "--directory" or not _segment_passes_directory(tokens, index):
                directory = _resolve(directory, inline or following or "")
        elif option == "PYTHONPATH" and inline:
            pythonpath = tuple(
                _resolve(directory, entry or ".") for entry in inline.split(os.pathsep)
            )
        elif token in _INI_OVERRIDE_OPTIONS and following is not None:
            pythonpath = _ini_pythonpath(following, directory) or pythonpath
        elif token.startswith(_INI_OVERRIDE_PREFIXES):
            value = token.removeprefix("--override-ini=").removeprefix("-o")
            pythonpath = _ini_pythonpath(value, directory) or pythonpath
        index += 1
    return directory, pythonpath


def _starts_segment(tokens: Sequence[str], index: int) -> bool:
    return index == 0 or tokens[index - 1] in _SHELL_SEPARATORS


def _segment_passes_directory(tokens: Sequence[str], index: int) -> bool:
    """Whether the shell segment holding ``tokens[index]`` passes uv's ``--directory``."""
    start = index
    while start and tokens[start - 1] not in _SHELL_SEPARATORS:
        start -= 1
    end = index
    while end < len(tokens) and tokens[end] not in _SHELL_SEPARATORS:
        end += 1
    return any(token.partition("=")[0] == "--directory" for token in tokens[start:end])


def _ini_pythonpath(override: str, directory: str | None) -> tuple[str, ...]:
    key, _, value = override.partition("=")
    if key.strip() != "pythonpath":
        return ()
    return tuple(_resolve(directory, entry) for entry in value.split())


def _resolve(directory: str | None, value: str) -> str:
    path = os.path.expanduser(value)
    if directory is not None:
        path = os.path.join(directory, path)
    return os.path.normpath(path)
