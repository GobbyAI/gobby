"""Bounded, conservative extraction of sed program write destinations."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SedExecution:
    commands: tuple[str, ...]
    reads_pattern: bool
    unknown_execution: bool
    parser_unknown: bool
    substitutes: bool
    pattern_modified: bool


@dataclass
class _ExecutionCollector:
    commands: list[str] = field(default_factory=list)
    reads_pattern: bool = False
    unknown_execution: bool = False
    substitutes: bool = False
    pattern_modified: bool = False


def _bracket_end(program: str, start: int) -> int | None:
    index = start + 1
    if index < len(program) and program[index] == "^":
        index += 1
    if index < len(program) and program[index] == "]":
        index += 1
    while index < len(program):
        if program[index] == "\\":
            index += 2
        elif program[index] == "]":
            return index + 1
        elif program[index] == "[" and index + 1 < len(program) and program[index + 1] in ":=.":
            end = program.find(program[index + 1] + "]", index + 2)
            if end < 0:
                return None
            index = end + 2
        else:
            index += 1
    return None


def _delimited_end(
    program: str, start: int, delimiter: str, *, regex_brackets: bool = False
) -> int | None:
    index = start
    while index < len(program):
        if program[index] == "\\":
            index += 2
        elif program[index] == delimiter:
            return index + 1
        elif regex_brackets and program[index] == "[":
            end = _bracket_end(program, index)
            if end is None:
                return None
            index = end
        else:
            index += 1
    return None


def _address_end(program: str, start: int, regex_brackets: bool) -> int | None:
    index = start
    if index >= len(program):
        return index
    if program[index].isdigit() or program[index] in "+~":
        while index < len(program) and (program[index].isdigit() or program[index] in "+~"):
            index += 1
        return index
    if program[index] == "$":
        return index + 1
    if program[index] == "/" or program[index] == "\\":
        if program[index] == "\\":
            index += 1
            if index >= len(program):
                return None
        end = _delimited_end(program, index + 1, program[index], regex_brackets=regex_brackets)
        if end is not None:
            while end < len(program) and program[end] in "IM":
                end += 1
        return end
    return index


def _line_end(program: str, start: int) -> int:
    end = program.find("\n", start)
    return len(program) if end < 0 else end


def sed_write_paths(program: str, *, bsd: bool = False) -> tuple[list[str], bool]:
    """Union GNU and BSD regex readings; either unproven reading fails closed."""
    plain_paths, plain_unknown = _program_write_paths(program, regex_brackets=False, bsd=bsd)
    bracket_paths, bracket_unknown = _program_write_paths(program, regex_brackets=True, bsd=bsd)
    return list(dict.fromkeys([*plain_paths, *bracket_paths])), plain_unknown or bracket_unknown


def sed_execution(program: str) -> SedExecution:
    """Keep executable effects distinct from unrecognized non-executable grammar."""
    execution = _ExecutionCollector()
    unknown = False
    for brackets in (False, True):
        _, unproven = _program_write_paths(program, regex_brackets=brackets, execution=execution)
        unknown |= unproven
    return SedExecution(
        tuple(dict.fromkeys(execution.commands)),
        execution.reads_pattern,
        execution.unknown_execution or (execution.substitutes and execution.pattern_modified),
        unknown,
        execution.substitutes,
        execution.pattern_modified,
    )


def _program_write_paths(
    program: str,
    *,
    regex_brackets: bool,
    bsd: bool = False,
    execution: _ExecutionCollector | None = None,
) -> tuple[list[str], bool]:
    """Return destinations and whether any program behavior remains unproven.

    File arguments consume the rest of their line, including semicolons.
    Ambiguous grammar and executable `e` commands fail closed.
    """
    paths: list[str] = []
    index = 0
    while index < len(program):
        if program[index].isspace() or program[index] in ";{}":
            index += 1
            continue
        end = _address_end(program, index, regex_brackets)
        if end is None:
            return paths, not bsd
        index = end
        while index < len(program) and program[index] in " \t":
            index += 1
        if index < len(program) and program[index] == ",":
            index += 1
            while index < len(program) and program[index] in " \t":
                index += 1
            end = _address_end(program, index, regex_brackets)
            if end is None or end == index:
                return paths, not bsd
            index = end
        while index < len(program) and program[index] in " \t!":
            index += 1
        if index >= len(program):
            return paths, not bsd
        command = program[index]
        index += 1
        if command in "{}dDgGhHnNpPxz=F":
            if execution is not None and command in "gGhHNxz":
                execution.pattern_modified = True
            continue
        if command == "#":
            index = _line_end(program, index)
            continue
        if command in "sy":
            if index >= len(program) or program[index] in "\\\n":
                return paths, not bsd
            delimiter = program[index]
            index += 1
            sections: list[str] = []
            for section in range(2):
                begin = index
                end = _delimited_end(
                    program,
                    index,
                    delimiter,
                    regex_brackets=regex_brackets and command == "s" and section == 0,
                )
                if end is None:
                    return paths, not bsd
                sections.append(program[begin : end - 1])
                index = end
            if command == "y":
                if execution is not None:
                    execution.pattern_modified = True
                continue
            flags_start = index
            executes = False
            while index < len(program) and program[index] not in ";\n}":
                flag = program[index]
                if flag == "w":
                    break
                if flag == "e":
                    if execution is None:
                        return paths, not bsd
                    pattern, replacement = sections
                    executes = True
                    execution.substitutes = True
                    if "&" in replacement or "\\" in replacement:
                        execution.unknown_execution = True
                    else:
                        execution.commands.append(replacement)
                    execution.reads_pattern |= pattern != "^.*$"
                    index += 1
                    continue
                if not flag.isdigit() and flag not in "gpIiMm \t":
                    return paths, not bsd
                index += 1
            if execution is not None:
                if executes and any(flag in program[flags_start:index] for flag in "Mm"):
                    execution.unknown_execution = True
                if not executes:
                    execution.pattern_modified = True
            if index >= len(program) or program[index] != "w":
                continue
            index += 1
            command = "w"
        if command in "wW":
            end = _line_end(program, index)
            path = program[index:end].lstrip(" \t")
            if not path:
                return paths, not bsd
            if "\\" in path or "\x00" in path:
                return paths, True
            paths.append(path)
            index = end
        elif command in "rR":
            index = _line_end(program, index)
        elif command in "aic":
            # BSD text begins after backslash-newline; escaped newlines extend it.
            if program.startswith("\\\n", index):
                index += 2
            end = _line_end(program, index)
            while end < len(program):
                slash = end
                while slash > index and program[slash - 1] == "\\":
                    slash -= 1
                if (end - slash) % 2 == 0:
                    break
                index = end + 1
                end = _line_end(program, index)
            index = end
        elif command in ":btT":
            start = index
            while index < len(program) and program[index] not in ";\n":
                index += 1
            if len(program[start:index].split()) > 1:
                # GNU ends labels at whitespace; BSD may consume the whole line.
                return paths, True
        elif command in "qQlLv":
            while index < len(program) and (program[index].isdigit() or program[index] in " .\t"):
                index += 1
        elif command == "e":
            if execution is None:
                return paths, not bsd
            end = _line_end(program, index)
            operand = program[index:end].strip()
            if operand:
                execution.commands.append(operand)
            else:
                execution.reads_pattern = True
            index = end
        else:
            # Unsupported command letters fail during compilation. Keep any earlier
            # w targets (BSD may open them while compiling), but execution cannot
            # reach later commands. A literal file word such as f.py is not a script.
            return paths, False
    return list(dict.fromkeys(paths)), False
