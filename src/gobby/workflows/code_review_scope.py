"""Path scope of a Git commit, for the code-review gates.

``require-code-review-skill`` and ``require-code-review-self-review`` block a
review-gated Git operation until the ``code-review`` skill is loaded and an
``ocr delegate rule`` review has run since the last one. A commit that records
only documentation has nothing for that review to read: OCR excludes every such
path as ``unsupported_ext``, so the pass costs a skill load and two subprocesses
and reviews nothing.

The gates also exempt a clean, explicit fast-forward whose incoming commits
are already reachable from the registered main checkout's protected branch.
Every ambiguity stays gated: a missed gate lets unreviewed code land.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import psycopg

from gobby.config.shell_lexing import parse_shell_command
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.workflows.commit_guard import GitCommitInvocation, resolve_commit_inspect_cwd
from gobby.workflows.git_utils import resolve_git_worktree_root_async
from gobby.workflows.ledger_reconcile import session_dirty_file_set_for_checkout
from gobby.workflows.observer_utils import _extract_shell_command

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent
    from gobby.storage.hub.protocol import HubDatabase

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
    added_paths: tuple[str, ...] = ()
    """Literal paths a chained ``git add`` stages before an only-mode commit."""


@dataclass(frozen=True, slots=True)
class CommitReviewScope:
    """Reviewability plus the operation paths attributed to this session."""

    has_reviewable_paths: bool
    session_owned_reviewable_paths: tuple[str, ...] | None


def parse_commit_scope(command: str | None) -> CommitScope | None:
    """Return the paths a plain or narrowly wrapped `git commit` records.

    ``None`` means the recorded paths are not determinable from the command:
    another segment could stage or move files first, the subcommand is not
    ``commit``, or an option this module does not model is in play.
    """
    if not isinstance(command, str) or not command.strip():
        return None

    parsed = parse_shell_command(command)
    wrapper_chdir: str | None = None
    added_paths: tuple[str, ...] = ()
    if len(parsed.segments) == 1:
        tokens = list(parsed.segments[0])
    elif set(parsed.operators) == {"&&"} and len(parsed.segments) == len(parsed.operators) + 1:
        chained = _chained_add_paths(parsed.segments[:-1])
        if chained is None:
            return None
        added_paths = chained
        tokens = list(parsed.segments[-1])
    elif len(parsed.segments) == 3 and parsed.operators == ("&&", "|"):
        wrapper = re.fullmatch(r"(?s)\s*(.*?)\s+2>&1\s*\|\s*tail\s+-20\s*", command)
        if wrapper is None:
            return None
        prefix = wrapper.group(1)
        if prefix.count("&&") != 1 or any(
            char in prefix.replace("&&", "") for char in ";|&<>$`\\*?[]{}()\n\r"
        ):
            return None
        cd, commit, tail = parsed.segments
        if len(cd) != 2 or cd[0] != "cd" or cd[1].startswith(("-", "~")):
            return None
        if tail != ("tail", "-20"):
            return None
        wrapper_chdir = cd[1]
        tokens = list(commit)
    else:
        return None

    if not tokens or tokens[0].rsplit("/", maxsplit=1)[-1] != "git":
        return None

    parsed_globals = _parse_git_global_options(tokens[1:])
    if parsed_globals is None:
        return None
    index, chdir = parsed_globals
    if wrapper_chdir is not None and chdir is not None:
        return None
    scope = _parse_commit_arguments(tokens[1 + index :], wrapper_chdir or chdir)
    if scope is None or not added_paths:
        return scope
    # Only mode ignores whatever else is staged, so earlier adds can change the
    # recorded set only by making pathspec'd untracked paths trackable. A chain
    # ending in any other commit form records a staged set this cannot predict.
    if chdir is not None or not scope.pathspecs or scope.include_staged:
        return None
    return replace(scope, added_paths=added_paths)


def _chained_add_paths(segments: Sequence[tuple[str, ...]]) -> tuple[str, ...] | None:
    """Return the literal paths of plain ``git add`` segments, or None for any other shape."""
    paths: list[str] = []
    for segment in segments:
        if segment[:2] != ("git", "add"):
            return None
        operands = segment[3:] if segment[2:3] == ("--",) else segment[2:]
        if not operands or any(
            operand.startswith(("-", ":")) or any(char in operand for char in "*?[$`\\")
            for operand in operands
        ):
            return None
        paths.extend(operands)
    return tuple(paths)


def _parse_git_global_options(
    tokens: Sequence[str], *, subcommand: str = "commit"
) -> tuple[int, str | None] | None:
    """Return the index past the subcommand and git's ``-C`` directory, if any."""
    chdir: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == subcommand:
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
    review_scope = await inspect_commit_review_scope(event, project_path)
    return review_scope.has_reviewable_paths


async def inspect_commit_review_scope(
    event: HookEvent,
    project_path: str,
    variables: Mapping[str, object] | None = None,
    *,
    integration_branch: str | None = None,
    db: HubDatabase | None = None,
) -> CommitReviewScope:
    """Return reviewability and only the current session's review target paths.

    Unknown commit shapes stay gated, but expose no paths. This keeps a
    conservative gate from ever directing one session to review another
    session's staged work.
    """
    command = _extract_shell_command(event)
    scope = parse_commit_scope(command)
    if scope is None:
        if _parse_fast_forward(command) is not None:
            main: Path | None = None
            if db is not None and event.project_id:
                from gobby.tasks.land_commit import main_checkout_target

                try:
                    target = await main_checkout_target(db, event.project_id)
                except (RuntimeError, ValueError, OSError, psycopg.Error) as exc:
                    logger.debug("Code-review scope could not read the main checkout: %s", exc)
                    return CommitReviewScope(True, None)
                if target is None:
                    return CommitReviewScope(True, None)
                main, integration_branch = target
            if integration_branch and await _is_reviewed_fast_forward(
                event, project_path, integration_branch, main=main
            ):
                return CommitReviewScope(False, () if variables is not None else None)
        return CommitReviewScope(True, None)

    inspect_cwd = resolve_commit_inspect_cwd(
        GitCommitInvocation(pathspecs=(), chdir=scope.chdir),
        event_cwd=event.cwd if isinstance(event.cwd, str) else None,
        project_path=project_path,
    )
    paths = await _recorded_paths(scope, inspect_cwd)
    if not paths:
        return CommitReviewScope(True, () if variables is not None else None)

    reviewable = {
        path
        for path in paths
        if PurePosixPath(path).suffix.lower() not in NON_REVIEWABLE_EXTENSIONS
    }
    if variables is None:
        return CommitReviewScope(bool(reviewable), None)

    checkout_root = await resolve_git_worktree_root_async(inspect_cwd, project_path)
    if checkout_root is None:
        return CommitReviewScope(bool(reviewable), None)
    owned = session_dirty_file_set_for_checkout(variables, checkout_root)
    return CommitReviewScope(
        bool(reviewable),
        tuple(sorted(reviewable & owned)),
    )


def _parse_fast_forward(command: str | None) -> tuple[str, str | None] | None:
    """Recognize only an explicit, unchained fast-forward with one literal target."""
    if not command or any(char in command for char in ";|&<>$`\\*?[]{}()\n\r"):
        return None
    parsed = parse_shell_command(command)
    if len(parsed.segments) != 1 or parsed.operators:
        return None
    tokens = parsed.segments[0]
    if not tokens or tokens[0].rsplit("/", maxsplit=1)[-1] != "git":
        return None
    globals_ = _parse_git_global_options(tokens[1:], subcommand="merge")
    if globals_ is None:
        return None
    index, chdir = globals_
    arguments = tokens[index + 1 :]
    if "--ff-only" not in arguments:
        return None
    flags = {"--ff-only", "--quiet", "-q", "--no-edit", "--"}
    targets = [argument for argument in arguments if argument not in flags]
    if len(targets) != 1 or targets[0].startswith(("-", "~")):
        return None
    return targets[0], chdir


async def _is_reviewed_fast_forward(
    event: HookEvent, project_path: str, integration_branch: str, *, main: Path | None = None
) -> bool:
    """Prove a lone fast-forward imports no commits outside the integration tip."""
    parsed = _parse_fast_forward(_extract_shell_command(event))
    if parsed is None:
        return False
    target_ref, chdir = parsed
    cwd = resolve_commit_inspect_cwd(
        GitCommitInvocation(pathspecs=(), chdir=chdir),
        event_cwd=event.cwd if isinstance(event.cwd, str) else None,
        project_path=project_path,
    )
    if main is not None:
        common = await daemon_git.run(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd, timeout=10.0
        )
        if (
            not isinstance(common, GitOk)
            or Path(common.stdout.strip()).parent.resolve() != main.resolve()
        ):
            return False
    staged = await _diff_paths(cwd, ("--cached", "HEAD"), ())
    if staged is None or staged:
        return False
    commits: list[str] = []
    for ref in ("HEAD", f"{target_ref}^{{commit}}", f"refs/heads/{integration_branch}^{{commit}}"):
        resolved = await daemon_git.run(
            ["rev-parse", "--verify", "--end-of-options", ref], cwd=cwd, timeout=10.0
        )
        if not isinstance(resolved, GitOk):
            return False
        commit = resolved.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None:
            return False
        commits.append(commit)
    head, target, integration = commits
    ancestor = await daemon_git.run(
        ["merge-base", "--is-ancestor", head, target], cwd=cwd, timeout=10.0
    )
    if not isinstance(ancestor, GitOk):
        return False
    incoming = await daemon_git.run(
        ["rev-list", target, f"^{head}", f"^{integration}", "--"], cwd=cwd, timeout=10.0
    )
    return isinstance(incoming, GitOk) and not incoming.stdout.strip()


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
    if not scope.added_paths:
        return staged | worktree

    # A chained add makes untracked paths trackable; the commit records those
    # its pathspec also matches.
    pathspec_untracked = await _untracked_paths(cwd, scope.pathspecs)
    added_untracked = await _untracked_paths(cwd, scope.added_paths)
    if pathspec_untracked is None or added_untracked is None:
        return None
    return staged | worktree | (pathspec_untracked & added_untracked)


async def _untracked_paths(cwd: str, pathspecs: tuple[str, ...]) -> set[str] | None:
    """Repository-relative untracked, non-ignored paths matching ``pathspecs``."""
    args = ["ls-files", "--others", "--exclude-standard", "--full-name", "-z", "--", *pathspecs]
    result = await daemon_git.run(args, cwd=cwd, timeout=10.0)
    if not isinstance(result, GitOk):
        logger.debug(
            "Code-review scope inspection could not list untracked paths: %s",
            result.stderr.strip() or result.status,
        )
        return None
    return {path for path in result.stdout.split("\0") if path}


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
