"""Sed program, file operand, and mutation-scope classification."""

from pathlib import Path

from gobby.hooks._normalization_segments import _ShellSegmentMetadata
from gobby.hooks._normalization_shell import (
    _contains_unexpanded_shell_reference,
    _looks_file_like,
    _looks_path_target,
    _rebase_navigation_shell_paths,
    _rebase_shell_paths,
    _shell_positional_args,
)
from gobby.hooks._sed_arguments import _sed_file_operands, _SedOperands
from gobby.hooks._sed_program import sed_write_paths
from gobby.hooks.code_navigation import sed_line_count, source_read_navigation_metadata
from gobby.hooks.shell_execution import SCRIPT_READ_BUDGET, read_script_body


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
            defer_unresolved=True,
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
