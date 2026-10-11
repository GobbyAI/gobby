"""Git mutation operands and repository selection for shell normalization."""

from contextvars import ContextVar
from pathlib import Path

from gobby.hooks._normalization_operands import (
    _git_add_positional_args_after,
    _git_restore_positional_args_after,
)
from gobby.hooks._normalization_segments import _ShellSegmentMetadata
from gobby.hooks._normalization_shell import (
    _contains_unexpanded_shell_reference,
    _looks_path_target,
    _rebase_shell_paths,
    _strip_shell_wrappers,
)
from gobby.hooks.code_navigation import shell_command_name

exported_git_selector: ContextVar[bool] = ContextVar("exported_git_selector", default=False)


def git_selector_setup(raw_parts: list[str], parts: list[str]) -> bool:
    """Fail closed after shell-level selector assignments or declarations."""
    if not parts:
        words = raw_parts
    elif parts[0] in {"export", "declare", "typeset", "local", "readonly"}:
        words = parts[1:]
    else:
        return False
    return any(
        word.split("=", 1)[0].removesuffix("+") in {"GIT_DIR", "GIT_WORK_TREE"} for word in words
    )


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


def classify_git_mutation(
    parts: list[str], cwd: str | None, base_cwd: Path | None
) -> _ShellSegmentMetadata | None:
    """Classify Git writes; explicit selectors that lack proof fail closed."""
    index = 1
    git_cwd = cwd or (str(base_cwd) if base_cwd is not None else None)
    unknown = bool(git_cwd and _contains_unexpanded_shell_reference(git_cwd))
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
    operation = parts[index] if index < len(parts) else ""
    arguments = parts[index + 1 :]
    recovery = any(flag in arguments for flag in {"--quit", "--skip", "--continue", "--abort"})
    if operation == "apply":
        if any(flag in arguments for flag in {"--check", "--stat", "--numstat"}):
            return _ShellSegmentMetadata("execute")
        return _ShellSegmentMetadata("write", repo_mutation=True)
    if operation in {"add", "checkout", "restore"}:
        if unknown:
            return _ShellSegmentMetadata("write", repo_mutation=True)
        if operation == "add":
            candidates = _git_add_positional_args_after(parts, index + 1)
        elif "--" in arguments:
            candidates = arguments[arguments.index("--") + 1 :]
        elif operation == "restore":
            candidates = _git_restore_positional_args_after(parts, index + 1)
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
