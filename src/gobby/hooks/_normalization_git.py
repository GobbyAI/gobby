"""Git mutation operands and repository selection for shell normalization."""

from contextvars import ContextVar
from pathlib import Path

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
    _contains_unexpanded_shell_reference,
    _looks_path_target,
    _rebase_navigation_shell_paths,
    _rebase_shell_paths,
    _strip_shell_wrappers,
    scan_shell_command,
)
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


def git_selector_setup(raw_parts: list[str], parts: list[str]) -> bool:
    """Fail closed after shell-level selector assignments or declarations."""
    if parts[:1] == ["set"]:
        return any(
            word == "allexport"
            or (word.startswith("-") and not word.startswith("--") and "a" in word[1:])
            for word in parts[1:]
        )
    if parts[:1] == ["eval"]:
        try:
            tokens = scan_shell_command(" ".join(parts[1:])).tokens
        except ValueError:
            return True
        return any(
            any(
                word.split("=", 1)[0].removesuffix("+") in GIT_SELECTOR_ENV_VARS
                for word in (token.value for token in segment.tokens)
            )
            for segment in _split_shell_segments(tokens)
        )
    if not parts:
        words = raw_parts
    elif parts[0] in {"export", "declare", "typeset", "local", "readonly"}:
        words = parts[1:]
    else:
        return False
    return any(word.split("=", 1)[0].removesuffix("+") in GIT_SELECTOR_ENV_VARS for word in words)


def git_environment_parts(raw_parts: list[str], parts: list[str]) -> list[str]:
    """Preserve repository selectors lost when transparent wrappers are removed."""
    if not parts or shell_command_name(parts[0]) != "git":
        return parts
    selectors: list[str] = ["--git-dir=$GIT_DIR"] if exported_git_selector.get() else []
    prefix_length = len(raw_parts) - len(_strip_shell_wrappers(raw_parts))
    for word in raw_parts[:prefix_length]:
        name, separator, value = word.partition("=")
        if separator and name in {"GIT_DIR", "GIT_WORK_TREE"}:
            option = "--git-dir" if name == "GIT_DIR" else "--work-tree"
            selectors.append(f"{option}={value}")
    return [parts[0], *selectors, *parts[1:]]


def git_command_parts(
    parts: list[str], cwd: str | None, base_cwd: Path | None
) -> tuple[list[str], str | None, bool]:
    """Skip Git global options and resolve only proven directory selectors."""
    index = 1
    git_cwd = cwd or (str(base_cwd) if base_cwd is not None else None)
    unknown = bool(git_cwd and git_cwd.startswith(UNRESOLVED_DIRECTORY))
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
        elif word in {
            "--no-pager",
            "--paginate",
            "-p",
            "--literal-pathspecs",
            "--glob-pathspecs",
            "--noglob-pathspecs",
            "--icase-pathspecs",
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
    return [parts[0], *parts[index:]], git_cwd, unknown


def classify_git_mutation(
    parts: list[str], cwd: str | None, base_cwd: Path | None
) -> _ShellSegmentMetadata | None:
    """Classify Git writes; explicit selectors that lack proof fail closed."""
    parts, git_cwd, unknown = git_command_parts(parts, cwd, base_cwd)
    index = 1
    operation = parts[index] if index < len(parts) else ""
    arguments = parts[index + 1 :]
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
        elif git_cwd:
            # Without a path separator, checkout may replace the whole working tree.
            return _ShellSegmentMetadata("write", paths=(git_cwd,), repo_mutation=True)
        else:
            candidates = []
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
                extra={"canonical_edit_file_paths": []},
            )
        return _ShellSegmentMetadata("write", repo_mutation=True)
    return None
