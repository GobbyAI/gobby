"""Sed program, file operand, and mutation-scope classification."""

from gobby.hooks._normalization_bindings import _raw_shell_word_is_literal
from gobby.hooks._normalization_segments import _ShellSegmentMetadata
from gobby.hooks._normalization_shell import (
    _contains_unexpanded_shell_reference,
    _has_sed_inplace_option,
    _looks_file_like,
    _looks_path_target,
    _rebase_navigation_shell_paths,
    _rebase_shell_paths,
    _shell_positional_args,
)
from gobby.hooks.code_navigation import sed_line_count, source_read_navigation_metadata


def classify_sed_segment(
    parts: list[str], cwd: str | None, raw_parts: tuple[str, ...] | None
) -> _ShellSegmentMetadata:
    positional = _shell_positional_args(parts)
    read_operands, write_operands, unresolved_script = _sed_file_operands(parts, raw_parts)
    if unresolved_script:
        # Expanded sed programs can contain file-writing commands.
        return _ShellSegmentMetadata("write", repo_mutation=True)
    if _has_sed_inplace_option(parts):
        paths = [item for item in write_operands if _looks_path_target(item)]
        rebased_paths = tuple(_rebase_shell_paths(paths, cwd))
        return _ShellSegmentMetadata(
            "write",
            paths=rebased_paths,
            write_paths=rebased_paths,
            repo_mutation=True,
        )
    paths = _rebase_navigation_shell_paths(
        [
            item
            for item in read_operands
            if _looks_file_like(item) or _contains_unexpanded_shell_reference(item)
        ],
        cwd,
    )
    line_count = sed_line_count(parts, positional)
    read_scope = "line_range" if line_count is not None else "full_file"
    return _ShellSegmentMetadata(
        "read",
        paths=tuple(paths),
        extra=source_read_navigation_metadata(
            paths,
            line_count=line_count,
            read_scope=read_scope,
        ),
    )


def _sed_file_operands(
    parts: list[str], raw_parts: tuple[str, ...] | None
) -> tuple[list[str], list[str], bool]:
    """Separate sed's program and script files from its input file operands."""
    positional: list[str] = []
    positional_indices: list[int] = []
    script_files: list[str] = []
    explicit_script = False
    unresolved_script = False
    after_options = False
    index = 1

    def unresolved(value: str, source_index: int) -> bool:
        return _contains_unexpanded_shell_reference(value) and not (
            raw_parts is not None and _raw_shell_word_is_literal(raw_parts[source_index])
        )

    while index < len(parts):
        source_index = index
        word = parts[index]
        index += 1
        if not after_options and word == "--":
            after_options = True
            continue
        if not after_options and word.startswith("--"):
            option, equals, value = word.partition("=")
            if option in {"--expression", "--file", "--line-length"}:
                if not equals and index < len(parts):
                    source_index = index
                    value = parts[index]
                    index += 1
                if option != "--line-length":
                    explicit_script = True
                    unresolved_script |= unresolved(value, source_index)
                    if option == "--file" and value:
                        script_files.append(value)
            continue
        if not after_options and word.startswith("-") and word != "-":
            for offset, option in enumerate(word[1:], start=1):
                if option in "efl":
                    value = word[offset + 1 :]
                    if not value and index < len(parts):
                        source_index = index
                        value = parts[index]
                        index += 1
                    if option != "l":
                        explicit_script = True
                        unresolved_script |= unresolved(value, source_index)
                        if option == "f" and value:
                            script_files.append(value)
                    break
                if option == "i":
                    # BSD accepts a separate empty or dot-prefixed backup suffix.
                    if (
                        offset == len(word) - 1
                        and index < len(parts)
                        and (parts[index] == "" or parts[index].startswith("."))
                    ):
                        index += 1
                    break
            continue
        positional.append(word)
        positional_indices.append(source_index)
    inputs = positional if explicit_script else positional[1:]
    if not explicit_script and positional:
        unresolved_script |= unresolved(positional[0], positional_indices[0])
    return script_files + inputs, inputs, unresolved_script
