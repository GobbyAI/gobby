"""Shell parsing and path selection for Git commit ownership checks."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from gobby.hooks._normalization_git import git_selector_name, git_selector_setup
from gobby.hooks._normalization_segments import _split_shell_segments
from gobby.hooks._normalization_shell import (
    _MAX_SUBSTITUTION_DEPTH,
    ShellToken,
    _contains_unexpanded_shell_reference,
    _literal_cd_target,
    _strip_shell_wrappers,
    scan_shell_command,
    shell_token_values,
    strip_input_redirections,
    strip_output_redirections,
)
from gobby.hooks._provider_shell_syntax import _prepare

_GIT_GLOBAL_OPTIONS_WITH_VALUE = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--exec-path",
}


@dataclass(frozen=True)
class GitCommitInvocation:
    """One Git commit invocation and its explicit pathspecs."""

    pathspecs: tuple[str, ...]
    chdir: str | None = None
    work_tree: str | None = None
    root_options: tuple[str, ...] = ()
    cwd_unverified: bool = False
    include_staged: bool = False
    all_tracked: bool = False

    @property
    def is_path_scoped(self) -> bool:
        return bool(self.pathspecs)


def parse_git_commit_invocations(command: str) -> tuple[GitCommitInvocation, ...]:
    """Parse Git commit invocations from one shell command."""
    return _parse_git_commit_invocations(command, 0)


def _parse_git_commit_invocations(command: str, depth: int) -> tuple[GitCommitInvocation, ...]:
    if depth > _MAX_SUBSTITUTION_DEPTH:
        return (GitCommitInvocation(pathspecs=(), cwd_unverified=True),)
    if not command.strip():
        return ()
    try:
        prepared, expansions = _prepare(command, depth)
        scan = scan_shell_command(prepared)
        shell_tokens = scan.tokens
        for heredoc in scan.heredocs:
            if not heredoc.quoted:
                expansions.extend(_prepare(heredoc.text, depth, data=True)[1])
        cwd_unverified = False
    except ValueError:
        # Bash may execute complete lines before rejecting malformed later
        # syntax. Splitting decoded words loses nested-shell quote provenance,
        # so it cannot prove that an executable prefix contains no commit.
        # Remove quoting/escaping conservatively: g\it and 'g'it still execute
        # Git, even when a later line cannot be tokenized.
        possible_words = command.replace("\\\n", "").translate(str.maketrans("", "", "\\'\""))
        # ANSI-C escapes and substitutions can compute a Git command name.
        # A failed scan cannot prove such expansions are harmless.
        return (
            (GitCommitInvocation(pathspecs=(), cwd_unverified=True),)
            if re.search(r"(?<![\w.-])git(?![\w.-])", possible_words)
            or any(char in command for char in "$`")
            else ()
        )

    invocations: list[GitCommitInvocation] = []
    if any(_parse_git_commit_invocations(text, depth + 1) for text in expansions):
        invocations.append(GitCommitInvocation(pathspecs=(), cwd_unverified=True))
    shell_chdir: str | None = None
    conditional_chdir = False
    selector_set = False
    segments = _split_shell_segments(_without_shell_comments(shell_tokens))
    for segment_index, segment in enumerate(segments):
        tokens = shell_token_values(
            strip_output_redirections(strip_input_redirections(segment.tokens))
        )
        if not tokens:
            continue
        selector_set |= git_selector_setup(tokens, _strip_shell_wrappers(tokens))
        next_separator = (
            segments[segment_index + 1].separator_before
            if segment_index + 1 < len(segments)
            else None
        )
        if conditional_chdir and segment.separator_before != "&&":
            # An earlier failure can skip conditional navigation while a later
            # unconditional command still runs in the original checkout.
            cwd_unverified = True
        if shell_chdir and segment.separator_before in {"||", "&"}:
            cwd_unverified = True
        if _unmodeled_navigation(segment.tokens):
            # Subshells, groups, compounds, functions, substitutions and the
            # other navigation forms below can move a later commit somewhere the
            # linear cd model cannot follow. Refuse rather than guess.
            cwd_unverified = True
        if any(
            _parse_git_commit_invocations(text, depth + 1)
            for text in _nested_shell_texts(segment.tokens)
        ):
            # A commit inside a nested shell string runs where that string says.
            invocations.append(
                GitCommitInvocation(
                    pathspecs=(), chdir=None, work_tree=None, root_options=(), cwd_unverified=True
                )
            )
        if tokens[0] == "cd":
            # Pipeline/background cd runs in a child shell and cannot rebase
            # later commands in the parent. Dynamic targets are unprovable.
            if segment.separator_before != "|" and next_separator not in {"|", "&"}:
                target = _literal_cd_target(tokens)
                if (
                    target is None
                    or _unproven_commit_path(target)
                    or segment.separator_before == "||"
                ):
                    # The tokenizer's quoted bit also covers partial quoting,
                    # which does not prove a leading tilde stayed literal.
                    cwd_unverified = True
                else:
                    shell_chdir = _join_chdir(shell_chdir, target)
                    conditional_chdir |= segment.separator_before == "&&"
            continue

        for index, token in enumerate(tokens):
            if not _is_git_word(token):
                continue
            commit_index, chdir, work_tree, root_options = _skip_git_global_options(
                tokens, index + 1
            )
            if commit_index >= len(tokens) or tokens[commit_index] != "commit":
                continue
            arguments = tokens[commit_index + 1 :]
            pathspecs, include_staged, all_tracked, arguments_unverified = _commit_arguments(
                arguments
            )
            if shell_chdir:
                # Prepend shell navigation to Git's own -C options so root
                # discovery and relative pathspec inspection use the same cwd.
                root_options = ("-C", shell_chdir, *root_options)
                chdir = _join_chdir(shell_chdir, chdir) if chdir else shell_chdir
            invocations.append(
                GitCommitInvocation(
                    pathspecs=pathspecs,
                    chdir=chdir,
                    work_tree=work_tree,
                    root_options=root_options,
                    include_staged=include_staged,
                    all_tracked=all_tracked,
                    cwd_unverified=cwd_unverified
                    or selector_set
                    or arguments_unverified
                    or "--icase-pathspecs" in tokens[index + 1 : commit_index]
                    or any(git_selector_name(word) for word in tokens[:index])
                    or _config_selector_option(tokens[index + 1 : commit_index])
                    or any(
                        path.startswith(":") or any(char in path for char in "*?[]{}\\")
                        for path in pathspecs
                    )
                    or any(
                        _unproven_commit_path(path)
                        for path in (chdir, work_tree, *root_options, *pathspecs)
                        if path is not None
                    ),
                )
            )
            break

    return tuple(invocations)


def _config_selector_option(options: list[str]) -> bool:
    """Configuration can select a different worktree before a commit."""
    for index, option in enumerate(options):
        if option == "-c" and index + 1 < len(options):
            value = options[index + 1]
        elif option.startswith("-c"):
            value = option[2:]
        elif option.startswith("--config-env="):
            value = option.partition("=")[2]
        else:
            continue
        if value.partition("=")[0].lower() == "core.worktree" or git_selector_name(value):
            return True
    return False


_COMMIT_VALUE_OPTIONS = frozenset(
    {
        "--message",
        "--file",
        "--reuse-message",
        "--reedit-message",
        "--author",
        "--date",
        "--fixup",
        "--squash",
        "--template",
        "--cleanup",
        "--trailer",
    }
)
_COMMIT_FLAG_OPTIONS = frozenset(
    {
        "--only",
        "--amend",
        "--no-edit",
        "--edit",
        "--allow-empty",
        "--allow-empty-message",
        "--no-verify",
        "--quiet",
        "--verbose",
        "--signoff",
        "--no-signoff",
        "--reset-author",
        "--no-post-rewrite",
        "--status",
        "--no-status",
        "--no-gpg-sign",
        "--dry-run",
        "--short",
        "--branch",
        "--porcelain",
        "--long",
        "--null",
    }
)


def _commit_arguments(arguments: list[str]) -> tuple[tuple[str, ...], bool, bool, bool]:
    """Model Git's positional paths and option values, refusing unknown selection semantics."""
    paths: list[str] = []
    include_staged = all_tracked = unverified = False
    index = 0
    while index < len(arguments):
        word = arguments[index]
        index += 1
        if word == "--":
            paths.extend(arguments[index:])
            break
        if not word.startswith("-") or word == "-":
            paths.append(word)
            continue
        if word.startswith("--"):
            option, equals, _value = word.partition("=")
            if option in _COMMIT_VALUE_OPTIONS:
                if not equals:
                    unverified |= index >= len(arguments)
                    index += 1
            elif option in {"--gpg-sign", "--untracked-files"}:
                pass  # Optional values must be attached with '='.
            elif option == "--all" and not equals:
                all_tracked = True
            elif option == "--include" and not equals:
                include_staged = True
            elif option not in _COMMIT_FLAG_OPTIONS or equals:
                # Includes file-fed pathspecs and their NUL mode.
                unverified = True
            continue
        for position, flag in enumerate(word[1:], start=1):
            if flag in "mFCct":
                if position == len(word) - 1:
                    unverified |= index >= len(arguments)
                    index += 1
                break  # The remaining short-option word is the value.
            if flag in "Su":
                break  # Optional values attach directly, never consume a path.
            if flag == "a":
                all_tracked = True
            elif flag == "i":
                include_staged = True
            elif flag not in "oqvnsez":
                unverified = True
    return tuple(paths), include_staged, all_tracked, unverified


_SHELL_KEYWORDS = frozenset(
    {"if", "then", "elif", "else", "fi", "while", "until", "do", "done"}
    | {"for", "case", "esac", "select", "function"}
)
_NAVIGATION_WORDS = frozenset({"cd", "pushd", "popd"})
_NESTED_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_SHELL_C_FLAG = re.compile(r"-[a-zA-Z]*c[a-zA-Z]*")
_COMMAND_WORD_PREFIX = re.compile(r"[({`]")


def _without_shell_comments(tokens: list[ShellToken]) -> list[ShellToken]:
    """Drop each unquoted ``#`` word and the rest of its line; keep the newline."""
    kept: list[ShellToken] = []
    in_comment = False
    for token in tokens:
        if in_comment and not (token.value == "\n" and not token.quoted):
            continue
        in_comment = not token.quoted and token.value.startswith("#")
        if not in_comment:
            kept.append(token)
    return kept


def _is_git_word(token: str) -> bool:
    """Whether a word runs git, including one opening a subshell or substitution."""
    return _COMMAND_WORD_PREFIX.split(token)[-1].rsplit("/", maxsplit=1)[-1] == "git"


def _unmodeled_navigation(tokens: list[ShellToken]) -> bool:
    """Whether a segment may change the cwd in a way the linear cd model cannot follow."""
    values = shell_token_values(tokens)
    head = _strip_shell_wrappers(values)
    if (
        values[0] in _SHELL_KEYWORDS
        or values[0] in {"pushd", "popd"}
        or (head and head[0] in {"eval", "source", "."})
    ):
        return True
    for position, token in enumerate(tokens):
        if token.value.startswith("CDPATH=") or (position and token.value in _NAVIGATION_WORDS):
            return True
        if not token.quoted and (
            token.value in {"{", "}"}
            or any(char in token.value for char in "()`")
            or token.value.startswith(("__gobby_group_", "__gobby_process_"))
            or token.value == "__gobby_expansion__"
        ):
            return True
    # Position-independent so assignments and wrappers (`command env`, `FOO=x env`)
    # cannot hide env's directory option ahead of git.
    git_index = next((i for i, value in enumerate(values) if _is_git_word(value)), None)
    head = values[:git_index]
    return any(value.rsplit("/", maxsplit=1)[-1] == "env" for value in head) and any(
        value.startswith(("-C", "--chdir")) for value in head
    )


def _unproven_commit_path(path: str) -> bool:
    """Decoded shell expansion markers are never literal ownership targets."""
    return (
        _contains_unexpanded_shell_reference(path)
        or path.startswith("~")
        or any(
            marker in path
            for marker in ("__gobby_expansion__", "__gobby_group_", "__gobby_process_")
        )
    )


def _nested_shell_texts(tokens: list[ShellToken]) -> list[str]:
    """Shell source this segment hands to another shell parse."""
    values = shell_token_values(tokens)
    # Any position, so assignments and wrappers (env, command, timeout, xargs)
    # ahead of the shell cannot hide its source from the parse.
    for index, value in enumerate(values):
        name = value.rsplit("/", maxsplit=1)[-1]
        if name == "eval" or (
            name in _NESTED_SHELLS
            and any(_SHELL_C_FLAG.fullmatch(later) for later in values[index + 1 :])
        ):
            return values[index + 1 :]
    # Expansions and groups are extracted from raw source by _prepare, before
    # token decoding discards the quotes that distinguish data from execution.
    return []


def _skip_git_global_options(
    tokens: list[str], index: int
) -> tuple[int, str | None, str | None, tuple[str, ...]]:
    chdir: str | None = None
    work_tree: str | None = None
    root_options: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index + 1, chdir, work_tree, tuple(root_options)
        if token == "-C" and index + 1 < len(tokens):
            chdir = _join_chdir(chdir, tokens[index + 1])
            root_options.extend(tokens[index : index + 2])
            index += 2
            continue
        if token.startswith("-C="):
            chdir = _join_chdir(chdir, token[3:])
            root_options.append(token)
            index += 1
            continue
        if token == "--work-tree" and index + 1 < len(tokens):
            work_tree = tokens[index + 1]
            root_options.extend(tokens[index : index + 2])
            index += 2
            continue
        if token.startswith("--work-tree="):
            work_tree = token.split("=", 1)[1]
            root_options.append(token)
            index += 1
            continue
        if token in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            if token == "--git-dir":
                root_options.extend(tokens[index : index + 2])
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in _GIT_GLOBAL_OPTIONS_WITH_VALUE):
            if token.startswith("--git-dir="):
                root_options.append(token)
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return index, chdir, work_tree, tuple(root_options)
    return index, chdir, work_tree, tuple(root_options)


def _join_chdir(current: str | None, nxt: str) -> str:
    if not current:
        return nxt
    nxt_path = Path(nxt)
    if nxt_path.is_absolute():
        return nxt
    return str(Path(current) / nxt_path)


def resolve_commit_inspect_cwd(
    invocation: GitCommitInvocation,
    *,
    event_cwd: str | None,
    project_path: str,
) -> str:
    """Working tree git should inspect for this commit, not the project default."""
    base = event_cwd or project_path
    if invocation.chdir:
        chdir = Path(invocation.chdir)
        base = str(chdir if chdir.is_absolute() else Path(base) / chdir)
    if invocation.work_tree:
        work_tree = Path(invocation.work_tree)
        base = str(work_tree if work_tree.is_absolute() else Path(base) / work_tree)
    return os.path.normpath(base)
