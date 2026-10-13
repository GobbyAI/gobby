"""Git mutation operands and repository selection for shell normalization."""

from contextvars import ContextVar
from pathlib import Path
from typing import Any

from gobby.hooks._normalization_env import UNRESOLVED_DIRECTORY
from gobby.hooks._normalization_operands import (
    _git_add_positional_args_after,
    _git_grep_is_revision_scoped,
    _git_restore_positional_args_after,
    _grep_is_recursive,
    _search_command_paths,
    _shell_positional_args_after,
)
from gobby.hooks._normalization_segments import _ShellSegmentMetadata, _split_shell_segments
from gobby.hooks._normalization_shell import (
    _SCRIPT_LIKE_CHARS,
    _contains_unexpanded_shell_reference,
    _looks_path_target,
    _rebase_navigation_shell_paths,
    _rebase_shell_paths,
    _strip_shell_wrappers,
    scan_shell_command,
)
from gobby.hooks._path_scope import checkout_root
from gobby.hooks.code_navigation import search_navigation_metadata, shell_command_name

exported_git_selector: ContextVar[bool] = ContextVar("exported_git_selector", default=False)

GIT_SELECTOR_ENV_VARS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_ICASE_PATHSPECS",
        "GIT_GLOB_PATHSPECS",
        "GIT_NOGLOB_PATHSPECS",
        "GIT_LITERAL_PATHSPECS",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    }
)


def classify_search_segment(
    cmd: str, parts: list[str], cwd: str | None, base_cwd: Path | None, *, shell_stdin: bool = False
) -> _ShellSegmentMetadata:
    """Classify rg/grep/git grep, naming an implicit search root the shell did not prove."""
    search_cwd, unknown = cwd, False
    if cmd == "git":
        parts, search_cwd, unknown = git_command_parts(parts, cwd, base_cwd)
        if len(parts) <= 1 or parts[1] != "grep":
            return _ShellSegmentMetadata("execute")
    paths = _rebase_navigation_shell_paths(_search_command_paths(cmd, parts), search_cwd)
    implicit_root = (
        cmd == "git"
        or (cmd == "rg" and (not shell_stdin or "--files" in parts[1:]))
        or (cmd == "grep" and _grep_is_recursive(parts))
    )
    if unknown or (
        implicit_root and not paths and (search_cwd or "").startswith(UNRESOLVED_DIRECTORY)
    ):
        paths = [UNRESOLVED_DIRECTORY]
    elif implicit_root and not paths and search_cwd:
        paths = [search_cwd]  # Preserve the implicit root after cd or an invocation wrapper.
    extra = search_navigation_metadata(paths)
    if shell_stdin and not paths and not implicit_root:
        # Stdin has no filesystem root, but scope still follows the shell's cwd.
        extra["_canonical_code_navigation_scope_paths"] = [search_cwd or str(base_cwd or ".")]
    if cmd == "git" and _git_grep_is_revision_scoped(parts):
        extra["canonical_search_revision_scoped"] = True
    return _ShellSegmentMetadata("search", paths=tuple(paths), extra=extra)


def git_selector_name(word: str) -> bool:
    """Recognize selectors and names whose expansion cannot be proven harmless."""
    name = word.split("=", 1)[0].removesuffix("+")
    return (
        name in GIT_SELECTOR_ENV_VARS
        or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
        or "__gobby_expansion__" in name
        or any(char in name for char in "$`{}*?[")
    )


def git_selector_setup(raw_parts: list[str], parts: list[str]) -> bool:
    """Fail closed after shell-level selector assignments or declarations."""
    if parts[:1] and parts[0] in {"set", "setopt", "unsetopt", "shopt"}:
        words = parts[1:]
        if parts[0] == "set" and "--" in words:
            words = words[: words.index("--")]
        return any(
            word.lower().replace("_", "").removeprefix("no") in {"allexport", "keyword"}
            or (
                parts[0] != "shopt"
                and word.startswith(("-", "+"))
                and word not in {"-", "+", "--"}
                and any(char in word[1:] for char in "ak")
            )
            for word in words
        )
    if parts[:1] == ["eval"]:
        try:
            tokens = scan_shell_command(" ".join(parts[1:])).tokens
        except ValueError:
            return True
        return any(
            any(git_selector_name(word) for word in (token.value for token in segment.tokens))
            for segment in _split_shell_segments(tokens)
        )
    if not parts:
        words = raw_parts
    elif parts[0] in {"export", "declare", "typeset", "local", "readonly", "integer", "float"}:
        words = [word for word in parts[1:] if not word.startswith(("-", "+"))]
    else:
        return False
    return any(git_selector_name(word) for word in words)


def git_environment_parts(raw_parts: list[str], parts: list[str]) -> list[str]:
    """Preserve repository selectors lost when transparent wrappers are removed."""
    if not parts or shell_command_name(parts[0]) != "git":
        return parts
    selectors: list[str] = ["--git-dir=$GIT_DIR"] if exported_git_selector.get() else []
    prefix_length = len(raw_parts) - len(_strip_shell_wrappers(raw_parts))
    for word in raw_parts[:prefix_length]:
        if not git_selector_name(word):
            continue
        name, separator, value = word.partition("=")
        name = name.removesuffix("+")
        if separator and name == "GIT_ICASE_PATHSPECS":
            selectors.append("--icase-pathspecs")
        elif separator and name in {"GIT_DIR", "GIT_WORK_TREE"}:
            option = "--git-dir" if name == "GIT_DIR" else "--work-tree"
            selectors.append(f"{option}={value}")
        else:
            selectors.append("--git-dir=$GIT_DIR")
    return [parts[0], *selectors, *parts[1:]]


def git_command_parts(
    parts: list[str], cwd: str | None, base_cwd: Path | None
) -> tuple[list[str], str | None, bool]:
    """Skip Git global options and resolve only proven directory selectors."""
    index = 1
    git_cwd = cwd or (str(base_cwd) if base_cwd is not None else None)
    unknown = bool(git_cwd and git_cwd.startswith(UNRESOLVED_DIRECTORY))
    icase_pathspecs = False
    while index < len(parts):
        word = parts[index]
        if word in {"-C", "-c"}:
            if index + 1 >= len(parts):
                unknown = True
                break
            value = parts[index + 1]
            if word == "-C":
                if value:
                    git_cwd = _rebase_shell_paths([value], git_cwd)[0]
                unknown = unknown or _contains_unexpanded_shell_reference(value)
            elif value.split("=", 1)[0].lower() == "core.worktree":
                unknown = True
            index += 2
        elif word == "--icase-pathspecs":
            icase_pathspecs = True
            index += 1
        elif word in {
            "--no-pager",
            "--paginate",
            "-p",
            "--literal-pathspecs",
            "--glob-pathspecs",
            "--noglob-pathspecs",
            "--no-optional-locks",
            "--no-replace-objects",
        }:
            index += 1
        elif word.startswith("-"):
            # Resolving GIT_DIR needs Git config/worktree metadata, not a cwd guess.
            unknown = True
            index += 2 if word in {"--git-dir", "--work-tree", "--namespace", "--exec-path"} else 1
        else:
            break
    unknown = unknown or (
        icase_pathspecs
        and index < len(parts)
        and parts[index] in {"checkout", "restore", "rm", "mv"}
    )
    return [parts[0], *parts[index:]], git_cwd, unknown


def classify_git_mutation(
    parts: list[str], cwd: str | None, base_cwd: Path | None
) -> _ShellSegmentMetadata | None:
    """Classify Git writes; explicit selectors that lack proof fail closed."""
    parts, git_cwd, unknown = git_command_parts(parts, cwd, base_cwd)
    index = 1
    operation = parts[index] if index < len(parts) else ""
    arguments = parts[index + 1 :]
    if operation == "switch" or (operation == "checkout" and "--" not in arguments):
        operands = _shell_positional_args_after(
            parts, index + 1, option_args={"-b", "-B", "--orphan"}
        )
        if operation == "checkout" and any(
            operand.startswith("~") or any(character in operand for character in _SCRIPT_LIKE_CHARS)
            for operand in operands
        ):
            return _ShellSegmentMetadata("write", repo_mutation=True)
        if operation == "checkout" and any(
            argument in {"-p", "--patch", "--pathspec-from-file"}
            or argument.startswith("--pathspec-from-file=")
            or (argument.startswith("-") and not argument.startswith("--") and "p" in argument[1:])
            or any(character in argument for character in "*?[")
            or argument.startswith(":")
            or _contains_unexpanded_shell_reference(argument)
            for argument in arguments
        ):
            return _ShellSegmentMetadata("write", repo_mutation=True)
        forced = any(
            argument in {"--force", "--discard-changes", "--merge"}
            or (
                argument.startswith("-")
                and not argument.startswith("--")
                and any(flag in argument[1:] for flag in "fm")
            )
            for argument in arguments
        )
        head_checkout = operation == "checkout" and arguments == ["HEAD"]
        root = checkout_root(Path(git_cwd).resolve()) if git_cwd and not unknown else None
        if root is None:
            if (operation == "switch" or head_checkout) and not forced and not unknown:
                return _ShellSegmentMetadata("execute")
            return _ShellSegmentMetadata("write", repo_mutation=True)
        extra: dict[str, Any] = {"canonical_edit_file_paths": []}
        if forced:
            extra["canonical_git_force_checkout_roots"] = [str(root)]
        # A positional checkout operand can be either a revision or a path,
        # even a tracked file deleted on disk. Retain checkout ownership checks
        # when only the containing worktree root can be proven.
        checked_paths = (
            operation == "checkout"
            and bool(operands)
            and not head_checkout
            and not any(argument in {"-b", "-B", "--orphan"} for argument in arguments)
        )
        if forced or checked_paths:
            extra["canonical_git_checkout_mutation"] = True
        positional_paths = tuple(_rebase_shell_paths(operands, git_cwd)) if checked_paths else ()
        # Branch scope is the worktree root, but non-force ownership checks
        # retain the literal operands so Git can handle branch conflicts.
        extra["canonical_dirty_edit_paths"] = list(positional_paths)
        path_checkout = any(Path(path).exists() for path in positional_paths)
        return _ShellSegmentMetadata(
            "write" if path_checkout else "execute",
            paths=positional_paths if path_checkout else (str(root),),
            repo_mutation=forced or checked_paths,
            extra=extra,
        )
    if operation in {"rm", "mv"}:
        skip_value = False
        for argument in arguments:
            if skip_value:
                skip_value = False
            elif argument == "--":
                break
            elif argument == "--pathspec-from-file" and operation == "rm":
                skip_value = True
            elif argument == "--dry-run" or (
                argument.startswith("-") and not argument.startswith("--") and "n" in argument[1:]
            ):
                return _ShellSegmentMetadata("execute")
    recovery = any(flag in arguments for flag in {"--quit", "--skip", "--continue", "--abort"})
    if operation == "apply":
        if any(flag in arguments for flag in {"--check", "--stat", "--numstat"}):
            return _ShellSegmentMetadata("execute")
        return _ShellSegmentMetadata("write", repo_mutation=True)
    if operation in {"add", "checkout", "restore", "rm", "mv"}:
        if unknown:
            return _ShellSegmentMetadata("write", repo_mutation=True)
        if operation == "add":
            candidates = _git_add_positional_args_after(parts, index + 1)
        elif operation in {"rm", "mv"}:
            candidates = _shell_positional_args_after(
                parts,
                index + 1,
                option_args={"--pathspec-from-file"} if operation == "rm" else set(),
            )
        elif "--" in arguments:
            candidates = arguments[arguments.index("--") + 1 :]
        elif operation == "restore":
            candidates = _git_restore_positional_args_after(parts, index + 1)
        else:
            candidates = []
        if operation != "add" and any(
            any(character in candidate for character in "*?[")
            or candidate.startswith((":", "~"))
            or any(character in candidate for character in _SCRIPT_LIKE_CHARS)
            for candidate in candidates
        ):
            return _ShellSegmentMetadata("write", repo_mutation=True)
        paths = [
            word
            for word in candidates
            if _looks_path_target(word) or _contains_unexpanded_shell_reference(word)
        ]
        return _ShellSegmentMetadata(
            "execute" if operation == "add" else "write",
            paths=tuple(_rebase_shell_paths(paths, git_cwd)),
            repo_mutation=True,
        )
    if operation == "revert" or (operation == "cherry-pick" and recovery):
        if not unknown and git_cwd and recovery:
            return _ShellSegmentMetadata(
                "write",
                paths=(git_cwd,),
                repo_mutation=True,
                extra={
                    "canonical_edit_file_paths": [],
                    "canonical_git_sequencer_scope_paths": [git_cwd],
                },
            )
        return _ShellSegmentMetadata("write", repo_mutation=True)
    return None
