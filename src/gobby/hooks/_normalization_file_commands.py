"""Mutation scope for file-management shell commands."""

from pathlib import Path

from gobby.hooks._normalization_operands import (
    _file_command_operands,
    _file_transfer_paths,
    _sort_output_paths,
    _tar_extract_paths,
    _truncate_positional_paths,
)
from gobby.hooks._normalization_segments import _ShellSegmentMetadata
from gobby.hooks._normalization_shell import (
    _has_perl_inplace_option,
    _looks_path_target,
    _rebase_shell_paths,
    _shell_positional_args,
)


def classify_file_command(
    cmd: str, parts: list[str], cwd: str | None, base_cwd: Path | None
) -> _ShellSegmentMetadata:
    if cmd == "sort":
        writes, paths = _sort_output_paths(parts)
        if writes:
            rebased_paths = tuple(_rebase_shell_paths(paths, cwd))
            return _ShellSegmentMetadata(
                "write", paths=rebased_paths, write_paths=rebased_paths, repo_mutation=True
            )

    if cmd == "tar":
        extracts, paths, unknown = _tar_extract_paths(parts, cwd)
        if extracts:
            return _ShellSegmentMetadata(
                "write",
                paths=tuple(paths),
                write_paths=tuple(paths),
                repo_mutation=True,
                extra={"_canonical_repo_mutation_scope_unknown": True} if unknown else None,
            )

    if cmd == "perl" and _has_perl_inplace_option(parts):
        positional = _shell_positional_args(parts)
        candidate = positional[-1] if positional else None
        paths = [candidate] if candidate and _looks_path_target(candidate) else []
        rebased_paths = tuple(_rebase_shell_paths(paths, cwd))
        return _ShellSegmentMetadata(
            "write",
            paths=rebased_paths,
            write_paths=rebased_paths,
            repo_mutation=True,
        )

    if cmd == "tee":
        positional = _shell_positional_args(parts)
        paths = [candidate for candidate in positional if _looks_path_target(candidate)]
        rebased_paths = tuple(_rebase_shell_paths(paths, cwd))
        return _ShellSegmentMetadata(
            "write",
            paths=rebased_paths,
            write_paths=rebased_paths,
            repo_mutation=True,
        )

    if cmd == "touch":
        operands, _, _ = _file_command_operands(cmd, parts)
        paths = [candidate for candidate in operands if _looks_path_target(candidate)]
        rebased_paths = tuple(_rebase_shell_paths(paths, cwd))
        return _ShellSegmentMetadata(
            "write",
            paths=rebased_paths,
            write_paths=rebased_paths,
            repo_mutation=True,
        )

    if cmd in {"rm", "mkdir", "rmdir"}:
        operands, _, _ = _file_command_operands(cmd, parts)
        paths = [candidate for candidate in operands if _looks_path_target(candidate)]
        return _ShellSegmentMetadata(
            "write",
            paths=tuple(_rebase_shell_paths(paths, cwd)),
            repo_mutation=True,
        )

    if cmd in {"cp", "mv", "install", "ln"}:
        rebased_paths = tuple(_file_transfer_paths(cmd, parts, cwd, base_cwd))
        return _ShellSegmentMetadata(
            "write",
            paths=rebased_paths,
            write_paths=rebased_paths if cmd not in {"mv", "ln"} else (),
            repo_mutation=True,
        )

    if cmd == "truncate":
        paths = _rebase_shell_paths(_truncate_positional_paths(parts), cwd)
        return _ShellSegmentMetadata(
            "write",
            paths=tuple(paths),
            write_paths=tuple(paths),
            repo_mutation=True,
        )

    return _ShellSegmentMetadata("execute")
