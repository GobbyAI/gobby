"""Path scope of a Git commit, for the code-review gates.

``require-code-review-skill`` and ``require-code-review-self-review`` block a
review-gated Git operation until the ``code-review`` skill is loaded and an
``ocr delegate rule`` review has run since the last one. A commit that records
only documentation has nothing for that review to read: OCR excludes every such
path as ``unsupported_ext``, so the pass costs a skill load and two subprocesses
and reviews nothing.

This module answers one question for those rules: will the commit record a path
OCR would review? Every ambiguity answers yes. A missed gate lets an unreviewed
code change land; a redundant one costs one review pass.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from gobby.config.shell_lexing import parse_shell_command
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.workflows.commit_guard import GitCommitInvocation, resolve_commit_inspect_cwd
from gobby.workflows.observer_utils import _extract_shell_command

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent

logger = logging.getLogger(__name__)

# OCR keeps its supported-extension table compiled into its Go binary: there is
# no data file to read and no subcommand that prints it, and its runtime answer
# (`delegate preview`'s `unsupported_ext`) also covers unknown extensions and
# binaries, which stay gated here. So the set lives here, deliberately narrow.
# Every entry is verified `unsupported_ext` by `ocr delegate preview` (v1.12.5).
NON_REVIEWABLE_EXTENSIONS = frozenset({".md", ".mdx", ".txt", ".rst", ".adoc"})

# Git's own options, before the subcommand. Only those that cannot change which
# paths a commit records — or, for -C, that this module resolves itself.
_GIT_GLOBAL_FLAGS = frozenset({"--no-pager", "--paginate", "-p", "--no-optional-locks"})

# `git commit` options that take a separate value, so the value is never
# mistaken for a pathspec.
_COMMIT_VALUE_OPTIONS = frozenset(
    {
        "-m",
        "--message",
        "-F",
        "--file",
        "-C",
        "--reuse-message",
        "-c",
        "--reedit-message",
        "-t",
        "--template",
        "--author",
        "--date",
        "--cleanup",
        "--fixup",
        "--squash",
        "--trailer",
    }
)

# Options that take no value. `-S`/`-u` and their long forms accept an optional
# attached value, which the cluster reader below consumes.
_COMMIT_FLAGS = frozenset(
    {
        "-a",
        "--all",
        "-i",
        "--include",
        "-o",
        "--only",
        "-n",
        "--no-verify",
        "--verify",
        "-s",
        "--signoff",
        "--no-signoff",
        "-q",
        "--quiet",
        "-v",
        "--verbose",
        "-e",
        "--edit",
        "--no-edit",
        "-z",
        "--null",
        "-S",
        "--gpg-sign",
        "--no-gpg-sign",
        "-u",
        "--untracked-files",
        "--allow-empty",
        "--allow-empty-message",
        "--no-post-rewrite",
        "--reset-author",
        "--dry-run",
        "--short",
        "--long",
        "--branch",
        "--no-branch",
        "--porcelain",
        "--status",
        "--no-status",
    }
)

# Options that leave the recorded paths undeterminable here: interactive hunk
# selection, a pathspec file this module does not read, and --amend, which also
# re-records the parent commit's paths.
_COMMIT_UNDETERMINABLE_OPTIONS = frozenset(
    {
        "-p",
        "--patch",
        "--interactive",
        "--amend",
        "--pathspec-from-file",
        "--pathspec-file-nul",
    }
)

_SHORT_VALUE_OPTIONS = frozenset({"-m", "-F", "-C", "-c", "-t"})
_SHORT_OPTIONAL_VALUE_OPTIONS = frozenset({"-S", "-u"})


@dataclass(frozen=True)
class CommitScope:
    """What one `git commit` invocation would record."""

    pathspecs: tuple[str, ...]
    chdir: str | None
    all_tracked: bool
    """``-a``: every tracked path whose working tree differs from the index."""
    include_staged: bool
    """``-i``: the staged changes as well as the pathspec'd working-tree ones."""


def parse_commit_scope(command: str | None) -> CommitScope | None:
    """Return what a lone `git commit` shell command records, else ``None``.

    ``None`` means the recorded paths are not determinable from the command:
    another segment could stage or move files first, the subcommand is not
    ``commit``, or an option this module does not model is in play.
    """
    if not isinstance(command, str) or not command.strip():
        return None

    parsed = parse_shell_command(command)
    # One segment only. Anything else — `git add x.py && git commit`,
    # `cd other && git commit`, a second Git operation — decides the recorded
    # paths after this event is evaluated.
    if len(parsed.segments) != 1:
        return None

    tokens = list(parsed.segments[0])
    if not tokens or tokens[0].rsplit("/", maxsplit=1)[-1] != "git":
        return None

    parsed_globals = _parse_git_global_options(tokens[1:])
    if parsed_globals is None:
        return None
    index, chdir = parsed_globals
    return _parse_commit_arguments(tokens[1 + index :], chdir)


def _parse_git_global_options(tokens: Sequence[str]) -> tuple[int, str | None] | None:
    """Return the index past ``commit`` and git's ``-C`` directory, if any."""
    chdir: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == "commit":
            return index, chdir
        if token == "-C" and chdir is None and index < len(tokens):
            chdir = tokens[index]
            index += 1
            continue
        if token in _GIT_GLOBAL_FLAGS:
            continue
        return None
    return None


def _parse_commit_arguments(tokens: Sequence[str], chdir: str | None) -> CommitScope | None:
    pathspecs: list[str] = []
    flags: set[str] = set()
    index = 0
    end_of_options = False
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if end_of_options or not token.startswith("-") or token == "-":
            pathspecs.append(token)
            continue
        if token == "--":
            end_of_options = True
            continue
        if token.startswith("--"):
            name, separator, _value = token.partition("=")
            if name in _COMMIT_UNDETERMINABLE_OPTIONS:
                return None
            if name in _COMMIT_VALUE_OPTIONS:
                if not separator:
                    index += 1
                continue
            if separator or name not in _COMMIT_FLAGS:
                return None
            flags.add(name)
            continue
        cluster = _read_short_cluster(token)
        if cluster is None:
            return None
        cluster_flags, consumes_next = cluster
        flags |= cluster_flags
        if consumes_next:
            index += 1

    all_tracked = bool(flags & {"-a", "--all"})
    include_staged = bool(flags & {"-i", "--include"})
    # `git commit -a <pathspec>` and `-a -i` are errors git refuses, so a
    # command carrying them records nothing this module can predict.
    if all_tracked and (pathspecs or include_staged):
        return None
    return CommitScope(
        pathspecs=tuple(pathspecs),
        chdir=chdir,
        all_tracked=all_tracked,
        include_staged=include_staged and bool(pathspecs),
    )


def _read_short_cluster(token: str) -> tuple[set[str], bool] | None:
    """Expand a short option cluster into flags plus whether a value follows."""
    flags: set[str] = set()
    characters = token[1:]
    position = 0
    while position < len(characters):
        option = f"-{characters[position]}"
        position += 1
        if option in _COMMIT_UNDETERMINABLE_OPTIONS:
            return None
        if option in _SHORT_VALUE_OPTIONS:
            # The rest of the token is the value, or the value is the next one.
            return flags, position == len(characters)
        if option not in _COMMIT_FLAGS:
            return None
        if option in _SHORT_OPTIONAL_VALUE_OPTIONS and position < len(characters):
            return flags, False
        flags.add(option)
    return flags, False


async def commit_has_reviewable_paths(event: HookEvent, project_path: str) -> bool:
    """Whether the commit this event runs records a path OCR would review.

    ``True`` whenever the answer is not provably no: a non-commit command, an
    undeterminable path set, a Git failure, or nothing to commit at all.
    """
    scope = parse_commit_scope(_extract_shell_command(event))
    if scope is None:
        return True

    inspect_cwd = resolve_commit_inspect_cwd(
        GitCommitInvocation(pathspecs=(), chdir=scope.chdir),
        event_cwd=event.cwd if isinstance(event.cwd, str) else None,
        project_path=project_path,
    )
    paths = await _recorded_paths(scope, inspect_cwd)
    if not paths:
        return True
    return any(
        PurePosixPath(path).suffix.lower() not in NON_REVIEWABLE_EXTENSIONS for path in paths
    )


async def _recorded_paths(scope: CommitScope, cwd: str) -> set[str] | None:
    """Paths the commit would record, or ``None`` when Git could not answer."""
    # Only mode (a pathspec without -i) records the pathspec'd paths alone, so
    # the staged half narrows with it; -i and the pathspec-less forms record
    # every staged path.
    staged = await _diff_paths(cwd, ("--cached",), () if scope.include_staged else scope.pathspecs)
    if staged is None:
        return None
    if not (scope.all_tracked or scope.pathspecs):
        return staged

    # -a and a pathspec both record working-tree content that is not staged yet.
    worktree = await _diff_paths(cwd, (), scope.pathspecs)
    if worktree is None:
        return None
    return staged | worktree


async def _diff_paths(
    cwd: str,
    diff_args: tuple[str, ...],
    pathspecs: tuple[str, ...],
) -> set[str] | None:
    # --no-renames keeps both sides of a rename, so renaming code to a doc
    # extension cannot hide the code path it deletes.
    args = ["diff", "--name-only", "--no-renames", "-z", *diff_args]
    if pathspecs:
        args += ["--", *pathspecs]
    result = await daemon_git.run(args, cwd=cwd, timeout=10.0)
    if not isinstance(result, GitOk):
        logger.debug(
            "Code-review scope inspection could not read commit paths: %s",
            result.stderr.strip() or result.status,
        )
        return None
    return {path for path in result.stdout.split("\0") if path}
