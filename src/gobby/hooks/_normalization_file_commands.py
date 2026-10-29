"""Mutation scope for file-management shell commands."""

from pathlib import Path

from gobby.hooks._normalization_archives import classify_archive_command, classify_ditto_command
from gobby.hooks._normalization_operands import (
    _SORT_VALUE_OPTIONS,
    _file_command_operands,
    _file_transfer_paths,
    _resolve_long_option,
    _shell_positional_args_after,
    _sort_output_paths,
    _truncate_positional_paths,
)
from gobby.hooks._normalization_segments import _ShellSegmentMetadata
from gobby.hooks._normalization_shell import (
    _looks_path_target,
    _rebase_shell_paths,
    _shell_positional_args,
)
from gobby.hooks._perl_arguments import perl_inplace_operands


def classify_file_command(
    cmd: str, parts: list[str], cwd: str | None, base_cwd: Path | None
) -> _ShellSegmentMetadata:
    if cmd == "ditto":
        return classify_ditto_command(parts, cwd)
    archive = classify_archive_command(cmd, parts, cwd)
    if archive is not None:
        return archive

    if cmd == "uniq":
        value_options = {"-f", "--skip-fields", "-s", "--skip-chars", "-w", "--check-chars"}
        abbreviations = {
            part.partition("=")[0]
            for part in parts[1:]
            if _resolve_long_option(part.partition("=")[0], frozenset(value_options))
        }
        operands = _shell_positional_args_after(
            parts,
            1,
            option_args=value_options | abbreviations,
        )
        if len(operands) > 1 and operands[1] != "-":
            uniq_paths = tuple(_rebase_shell_paths([operands[1]], cwd))
            return _ShellSegmentMetadata(
                "write", paths=uniq_paths, write_paths=uniq_paths, repo_mutation=True
            )
        uniq_paths = tuple(_rebase_shell_paths([path for path in operands[:1] if path != "-"], cwd))
        return _ShellSegmentMetadata("read", paths=uniq_paths)

    if cmd == "sort":
        writes, paths = _sort_output_paths(parts)
        if writes:
            rebased_paths = tuple(_rebase_shell_paths(paths, cwd))
            return _ShellSegmentMetadata(
                "write", paths=rebased_paths, write_paths=rebased_paths, repo_mutation=True
            )
        return _ShellSegmentMetadata(
            "execute",
            read_only=not any(
                _resolve_long_option(part.partition("=")[0], _SORT_VALUE_OPTIONS)
                == "--compress-program"
                for part in parts[1:]
            ),
        )

    if cmd in {"cut", "jq", "tr"}:
        return _ShellSegmentMetadata("execute", read_only=True)

    inplace = perl_inplace_operands(parts) if cmd == "perl" else None
    if inplace is not None:
        paths = [candidate for candidate in inplace if _looks_path_target(candidate)]
        rebased_paths = tuple(dict.fromkeys(_rebase_shell_paths(paths, cwd)))
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
