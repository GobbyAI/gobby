"""Sed program, file operand, and mutation-scope classification."""

from dataclasses import dataclass
from pathlib import Path

from gobby.hooks._normalization_bindings import _raw_shell_word_is_literal
from gobby.hooks._normalization_segments import _ShellSegmentMetadata
from gobby.hooks._normalization_shell import (
    _contains_unexpanded_shell_reference,
    _looks_file_like,
    _looks_path_target,
    _rebase_navigation_shell_paths,
    _rebase_shell_paths,
    _shell_positional_args,
)
from gobby.hooks._sed_program import sed_write_paths
from gobby.hooks.code_navigation import sed_line_count, source_read_navigation_metadata
from gobby.hooks.shell_execution import SCRIPT_READ_BUDGET, read_script_body

_LONG_OPTIONS = (
    "--expression",
    "--file",
    "--in-place",
    "--line-length",
    "--quiet",
    "--silent",
    "--debug",
    "--follow-symlinks",
    "--help",
    "--posix",
    "--regexp-extended",
    "--sandbox",
    "--separate",
    "--unbuffered",
    "--version",
)


@dataclass(frozen=True)
class _SedOperands:
    inputs: tuple[str, ...]
    script_files: tuple[str, ...]
    programs: tuple[str, ...]
    unresolved: bool
    inplace: bool


def classify_sed_segment(
    parts: list[str], cwd: str | None, raw_parts: tuple[str, ...] | None, base_cwd: Path | None
) -> _ShellSegmentMetadata:
    operands = _sed_file_operands(parts, raw_parts)
    primary = _classify_sed_operands(parts, cwd, operands, base_cwd)
    bsd_operands = _sed_file_operands(parts, raw_parts, bsd_inplace_suffix=True)
    if operands == bsd_operands:
        return primary
    alternate = _classify_sed_operands(parts, cwd, bsd_operands, base_cwd, bsd=True)
    # A bare -i may consume any following word as its extension on BSD.
    # Keep both readings: a known scratch target must not hide the other's writes.
    writes = tuple(dict.fromkeys((*primary.write_paths, *alternate.write_paths)))
    unknown = (
        operands.unresolved
        or bsd_operands.unresolved
        or any(
            item.extra and item.extra.get("_canonical_repo_mutation_scope_unknown")
            for item in (primary, alternate)
        )
    )
    extra: dict[str, object] = {"_canonical_repo_mutation_scope_unknown": True} if unknown else {}
    if (
        bsd_operands.unresolved
        and not operands.unresolved
        and not (primary.extra and primary.extra.get("_canonical_repo_mutation_scope_unknown"))
        and not bsd_operands.script_files
        and len(bsd_operands.programs) == 1
    ):
        extra["_canonical_sed_bsd_program"] = bsd_operands.programs[0]
    return _ShellSegmentMetadata(
        "write",
        paths=writes,
        write_paths=writes,
        repo_mutation=True,
        extra=extra or None,
    )


def _classify_sed_operands(
    parts: list[str],
    cwd: str | None,
    operands: _SedOperands,
    base_cwd: Path | None,
    *,
    bsd: bool = False,
) -> _ShellSegmentMetadata:
    positional = _shell_positional_args(parts)
    read_operands = [*operands.script_files, *operands.inputs]
    paths = _rebase_navigation_shell_paths(
        [
            item
            for item in read_operands
            if _looks_file_like(item) or _contains_unexpanded_shell_reference(item)
        ],
        cwd,
    )
    line_count = (
        None if operands.unresolved or operands.script_files else sed_line_count(parts, positional)
    )
    navigation = (
        {}
        if operands.inplace
        else source_read_navigation_metadata(
            paths,
            line_count=line_count,
            read_scope="line_range" if line_count is not None else "full_file",
        )
    )
    if operands.unresolved:
        # Expanded sed programs can contain file-writing commands.
        return _ShellSegmentMetadata(
            "write",
            repo_mutation=True,
            extra=navigation or None,
            navigation_paths=tuple(paths) if navigation else (),
        )
    programs = list(operands.programs)
    unresolved = len(operands.script_files) > SCRIPT_READ_BUDGET
    script_base = base_cwd if cwd is None else Path(cwd) if Path(cwd).is_absolute() else None
    for word in operands.script_files[:SCRIPT_READ_BUDGET]:
        script = read_script_body(word, script_base) if word != "-" else None
        if script is None:
            unresolved = True
        else:
            programs.append(script[1])
    writes: list[str] = []
    for program in programs:
        destinations, unknown = sed_write_paths(program, bsd=bsd)
        writes.extend(destinations)
        unresolved |= unknown
    inplace = operands.inplace
    if inplace:
        writes = [item for item in operands.inputs if _looks_path_target(item)] + writes
    if inplace or writes or unresolved:
        write_paths = tuple(dict.fromkeys(_rebase_shell_paths(writes, cwd)))
        extra = dict(navigation)
        if unresolved:
            extra["_canonical_repo_mutation_scope_unknown"] = True
        return _ShellSegmentMetadata(
            "write",
            paths=write_paths,
            write_paths=write_paths,
            repo_mutation=True,
            extra=extra or None,
            navigation_paths=tuple(paths) if navigation else (),
        )
    return _ShellSegmentMetadata("read", paths=tuple(paths), extra=navigation)


def _sed_file_operands(
    parts: list[str], raw_parts: tuple[str, ...] | None, *, bsd_inplace_suffix: bool = False
) -> _SedOperands:
    """Separate sed's program and script files from its input file operands."""
    positional: list[str] = []
    positional_indices: list[int] = []
    script_files: list[str] = []
    programs: list[str] = []
    explicit_script = False
    unresolved_script = False
    inplace = False
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
            matches = [candidate for candidate in _LONG_OPTIONS if candidate.startswith(option)]
            if len(matches) == 1:
                option = matches[0]
            inplace |= option == "--in-place"
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
                    elif option == "--expression":
                        programs.append(value)
            continue
        if not after_options and word.startswith("-") and word != "-":
            for offset, option in enumerate(word[1:], start=1):
                if option == "l":
                    value = word[offset + 1 :]
                    if value.isdigit():
                        break
                    if not value and index < len(parts) and parts[index].isdigit():
                        index += 1
                        break
                    continue
                if option in "ef":
                    value = word[offset + 1 :]
                    if not value and index < len(parts):
                        source_index = index
                        value = parts[index]
                        index += 1
                    explicit_script = True
                    unresolved_script |= unresolved(value, source_index)
                    if option == "f" and value:
                        script_files.append(value)
                    elif option == "e":
                        programs.append(value)
                    break
                if option in "iI":
                    inplace = True
                    # GNU -i has an attached optional suffix; BSD consumes the next word.
                    if (
                        offset == len(word) - 1
                        and index < len(parts)
                        and (
                            bsd_inplace_suffix
                            or option == "I"
                            or parts[index] == ""
                            or parts[index].startswith(".")
                        )
                    ):
                        index += 1
                    break
            continue
        positional.append(word)
        positional_indices.append(source_index)
    inputs = positional if explicit_script else positional[1:]
    if not explicit_script and positional:
        unresolved_script |= unresolved(positional[0], positional_indices[0])
        programs.append(positional[0])
    return _SedOperands(
        tuple(inputs), tuple(script_files), tuple(programs), unresolved_script, inplace
    )
