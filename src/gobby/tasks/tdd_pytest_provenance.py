"""Verify assignment-bearing pytest source displays before trusting runtime frames."""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import PurePosixPath

from gobby.tasks.tdd_compacted_output import _PYTEST_FAILURE_HEADER_RE, _RTK_FAILURE_HEADER_RE
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)
from gobby.tasks.transcript_tool_arguments import PythonModuleCache, parse_python_module

_ASSIGNMENT = re.compile(r"[ \t]+=[ \t]+")
_SHORT = re.compile(r"(\S+\.py):(\d+): in (\w+)")
_LOCATION = re.compile(r"(\S+\.py):(\d+):(?:[ \t].*)?")
_DEFINITION = re.compile(r"    (?:async )?def \w+\(|    @")
_FAILURE_SECTION_BOUNDARY_RE = re.compile(
    r"^(?:_+\s+(?![_\s]+$)\S.*\s+_+|\s*\d+\.\s+\[FAIL\]\s+\S.*|"
    r"(?:FAILED|ERROR|PASSED|SKIPPED)\s+\S.*|"
    r"\s*(?:FAIL|PASS)\s+\S+::\S+\s+\S+\s*$|"
    r".*::\S+\s+(?:PASSED|FAILED|ERROR|SKIPPED)\b|"
    # Bound Rust sections too: the evaluator shares this renderer boundary.
    r"\s*(?:[A-Z][A-Z-]*|TRY \d+ [A-Z-]+)\s+\[\s*[\d.]+s\]|"
    r"-{3,4} (?:\S+ std(?:out|err) -{4}|STD(?:OUT|ERR):\s))",
    re.IGNORECASE,
)


def _failure_section(lines: list[str], start: int) -> str:
    end = min(len(lines), start + 120)
    for boundary in range(start + 1, end):
        if _FAILURE_SECTION_BOUNDARY_RE.match(lines[boundary]):
            end = boundary
            break
    return "\n".join(lines[start:end])


def runtime_pytest_stub_sections(
    output: str,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    cache: PythonModuleCache | None = None,
) -> tuple[tuple[str, str, str], ...]:
    """Return source-proven sections and their unambiguous terminal stub frames."""
    output = re.split(r"(?m)^-+\s+Captured (?:stdout|stderr|log)\b", output)[0]
    recalled = recalled_pytest_sections(output, evidence, run, cache)
    if recalled:
        return tuple(
            (header["name"], section, terminal)
            for section, terminal in recalled
            if (header := _PYTEST_FAILURE_HEADER_RE.fullmatch(section.splitlines()[0])) is not None
        )
    source_output = runtime_pytest_output(output, evidence, run, cache)
    if source_output is None:
        return ()
    lines = output.splitlines()
    sections: list[tuple[str, str, str]] = []
    for index, line in enumerate(lines):
        header = _PYTEST_FAILURE_HEADER_RE.match(line) or _RTK_FAILURE_HEADER_RE.match(line)
        if header is None:
            continue
        section = runtime_pytest_output(_failure_section(lines, index), evidence, run, cache)
        if section is None or section not in source_output:
            continue
        terminal = terminal_runtime_frame(section, "\n".join(lines[index:]))
        if terminal is not None:
            sections.append((header["name"], section, terminal))
    return tuple(sections)


def _snapshot(
    path: str, evidence: TranscriptEvidence, run: TranscriptValidationRun
) -> TranscriptEdit | None:
    latest: dict[str, TranscriptEdit] = {}
    for edit in evidence.edits:
        if edit.session_id != run.session_id or edit.timestamp >= run.started_at:
            continue
        previous = latest.get(edit.path)
        if previous is None or (edit.timestamp, edit.order) > (previous.timestamp, previous.order):
            latest[edit.path] = edit
    reported = PurePosixPath(path).parts
    matches = [
        edit
        for candidate, edit in latest.items()
        if reported[-len(PurePosixPath(candidate).parts) :] == PurePosixPath(candidate).parts
    ]
    if len(matches) != 1:
        return None
    edit = matches[0]
    if (
        not edit.task_attributed
        or not edit.source_confirmed
        or edit.source_confirmed_at is None
        or edit.source_confirmed_at >= run.started_at
        or edit.source_after is None
    ):
        return None
    return edit


def _functions(module: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _short_source_matches(
    frame: re.Match[str],
    row: str,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    cache: PythonModuleCache | None,
) -> bool:
    edit = _snapshot(frame[1], evidence, run)
    if edit is None or edit.source_after is None:
        return False
    module = parse_python_module(edit.source_after, cache)
    if module is None:
        return False
    functions = [node for node in _functions(module) if node.name == frame[3]]
    line = int(frame[2])
    if len(functions) != 1 or not functions[0].lineno <= line <= (functions[0].end_lineno or 0):
        return False
    source = edit.source_after.splitlines()
    # Pytest short style strips exactly this one located source row, then prefixes four spaces.
    # Locals after it cannot extend this span, even when they match another source row.
    return 0 < line <= len(source) and row == "    " + source[line - 1].strip()


def recalled_pytest_sections(
    output: str,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    cache: PythonModuleCache | None = None,
) -> tuple[tuple[str, str], ...]:
    """Bind RTK's dedented short reports without treating repr text as boundaries.

    A recall receipt binds the report to its original run. Each section must consist
    entirely of short frames and one terminal exception, with no locals, metadata,
    or exception continuations. Historical source proves its test and final frame;
    intermediate library frames provide no attribution. Captured output is cut off
    by the caller before parsing.
    """
    if (
        re.fullmatch(r"(?:uv run )?rtk recall [0-9a-f]{12,64}", run.output_recovered_from or "")
        is None
    ):
        return ()
    rows = output.splitlines()
    full_report = bool(rows and re.fullmatch(r"=+ test session starts =+", rows[0]))
    expected_failures: int | None = None
    if full_report:
        banners = [index for index, row in enumerate(rows) if re.fullmatch(r"=+ FAILURES =+", row)]
        summary = re.fullmatch(
            r"=+ (\d+) failed(?:, \d+ passed)? in \d+(?:\.\d+)?s(?: \([\d:]+\))? =+",
            rows[-1],
        )
        if len(banners) != 1 or summary is None:
            return ()
        start = banners[0]
        # A complete pytest envelope cannot carry earlier traceback/exception rows.
        if any(
            _PYTEST_FAILURE_HEADER_RE.fullmatch(row)
            or _SHORT.fullmatch(row)
            or row.startswith(("E ", "E\t"))
            for row in rows[:start]
        ):
            return ()
        expected_failures = int(summary[1])
        rows = rows[start + 1 : -1]
    sections: list[tuple[str, str]] = []
    failure_count = 0
    index = 0
    while index < len(rows):
        if not rows[index]:
            index += 1
            continue
        header = _PYTEST_FAILURE_HEADER_RE.fullmatch(rows[index])
        if header is None:
            return ()
        section = [rows[index]]
        failure_count += 1
        index += 1
        frames: list[tuple[re.Match[str], str]] = []
        while index < len(rows) and (frame := _SHORT.fullmatch(rows[index])) is not None:
            if index + 1 == len(rows):
                return ()
            source = rows[index + 1]
            rendered = source[4:] if full_report and source.startswith("    ") else source
            if (
                not rendered
                or rendered != rendered.strip()
                or rendered.startswith(("E ", "E\t"))
                or (full_report and not source.startswith("    "))
            ):
                return ()
            frames.append((frame, "    " + rendered))
            section.extend((rows[index], "    " + rendered))
            index += 2
            if index < len(rows) and re.fullmatch(
                r"[ \t]+\^+" if full_report else r"\^+", rows[index]
            ):
                index += 1
        if (
            len(frames) < 2
            or index == len(rows)
            or not re.fullmatch(r"E[ \t]+NotImplementedError(?::[^\n]*)?", rows[index])
        ):
            return ()
        section.append(rows[index])
        index += 1
        first, last = frames[0], frames[-1]
        name = header["name"].split("[", 1)[0].rsplit(".", 1)[-1]
        if (
            first[0][3] == name
            and _short_source_matches(*first, evidence, run, cache)
            and _short_source_matches(*last, evidence, run, cache)
        ):
            terminal = "\n".join((last[0][0], last[1], rows[index - 1]))
            # Only the first frame establishes node identity; a helper deeper in
            # a sibling's traceback cannot lend its path to the section header.
            sections.append(("\n".join(section[:3]), terminal))
    if expected_failures is not None and failure_count != expected_failures:
        return ()
    return tuple(sections)


def _long_source_matches(
    rows: list[str],
    start: int,
    end: int,
    location: re.Match[str],
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    cache: PythonModuleCache | None,
) -> bool:
    edit = _snapshot(location[1], evidence, run)
    if edit is None or edit.source_after is None:
        return False
    module = parse_python_module(edit.source_after, cache)
    if module is None:
        return False
    line = int(location[2])
    functions = _functions(module)
    matches = 0
    for function in functions:
        if not function.lineno <= line <= (function.end_lineno or 0):
            continue
        if sum(node.name == function.name for node in functions) != 1:
            continue
        statements = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.stmt) and node.lineno <= line <= (node.end_lineno or 0)
        ]
        statement = min(statements, key=lambda node: (node.end_lineno or 0) - node.lineno)
        first = min([function.lineno, *(node.lineno for node in function.decorator_list)])
        source = edit.source_after.splitlines()[first - 1 : statement.end_lineno]
        # This is pytest's known long-source deindent, never arbitrary row stripping.
        source = textwrap.dedent("\n".join(source)).splitlines()
        expected = [
            (">   " if first + offset == line else "    ") + row
            for offset, row in enumerate(source)
        ]
        displayed = rows[start:end]
        target = line - first
        # Pytest may add a caret-only highlight immediately after the executing row.
        if (
            len(displayed) == len(expected) + 1
            and 0 <= target + 1 < len(displayed)
            and re.fullmatch(r"[ \t]+\^+", displayed[target + 1])
        ):
            displayed = displayed[: target + 1] + displayed[target + 2 :]
        matches += displayed == expected
    return matches == 1


def _metadata_row(row: str) -> bool:
    return re.fullmatch(r"(?:\w+|\.0)[ \t]+=[ \t]+[^\r\n]*", row) is not None


def _parameter_names(
    rows: list[str],
    location: re.Match[str],
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    cache: PythonModuleCache | None,
) -> set[str] | None:
    definition = next(
        (match for row in rows if (match := re.match(r"    (?:async )?def (\w+)\(", row))),
        None,
    )
    edit = _snapshot(location[1], evidence, run)
    if definition is None or edit is None or edit.source_after is None:
        return None
    module = parse_python_module(edit.source_after, cache)
    if module is None:
        return None
    functions = [node for node in _functions(module) if node.name == definition[1]]
    if len(functions) != 1:
        return None
    arguments = functions[0].args
    return {
        node.arg
        for node in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            *([arguments.vararg] if arguments.vararg else []),
            *([arguments.kwarg] if arguments.kwarg else []),
        )
    }


def _long_frame(
    rows: list[str],
    start: int,
    arguments: list[str],
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    cache: PythonModuleCache | None,
) -> tuple[int, list[str]] | None:
    end = start + 1
    while end < len(rows) and rows[end].startswith(("    ", ">   ")):
        end += 1
    located = end
    details: list[str] = []
    # E-prefixed assertion explanations belong to this frame, including their
    # renderer-generated '=' rows. They cannot introduce column-zero source.
    while located < len(rows) and (not rows[located] or re.match(r"E[ \t]", rows[located])):
        details.append(rows[located])
        located += 1
    locals_rows: list[str] = []
    while located < len(rows) and _LOCATION.fullmatch(rows[located]) is None:
        row = rows[located]
        if row:
            # A source/def/header-looking continuation inside locals is data,
            # never the beginning of another frame.
            return None
        locals_rows.append(row)
        located += 1
    if located == len(rows):
        return None
    location = _LOCATION.fullmatch(rows[located])
    if location is None:
        return None
    if arguments or locals_rows or any(_ASSIGNMENT.search(row) for row in rows[start:end]):
        if not _long_source_matches(rows, start, end, location, evidence, run, cache):
            return None
    if arguments:
        parameters = _parameter_names(rows[start:end], location, evidence, run, cache)
        names = [match[1] for row in arguments for match in re.finditer(r"(?:^|, )(\w+) = ", row)]
        if parameters is None or not names or not set(names) <= parameters:
            return None
        if len(names) != len(set(names)):
            return None
    # Funcargs and locals values never reach the downstream frame recognizer.
    return located + 1, [*rows[start:end], *details, rows[located]]


def runtime_pytest_output(
    output: str,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    cache: PythonModuleCache | None = None,
) -> str | None:
    """Keep renderer source and exception regions, excluding frame metadata.

    Only positioned, contiguous source spans from confirmed pre-run snapshots exempt
    assignment separators. A later forged block cannot exempt a metadata continuation.
    The caller applies the first-Captured cutoff before passing the retained prefix.
    """
    rows = output.splitlines()
    trusted: list[str] = []
    header_name: str | None = None
    first_definition = True
    index = 0
    while index < len(rows):
        row = rows[index]
        header = _PYTEST_FAILURE_HEADER_RE.match(row)
        if header is not None:
            header_name = header["name"].split("[", 1)[0].rsplit(".", 1)[-1]
            first_definition = True
        short = _SHORT.fullmatch(row)
        if short is not None:
            index += 1
            if index == len(rows) or not rows[index].startswith("    "):
                return None
            if _ASSIGNMENT.search(rows[index]) and not _short_source_matches(
                short, rows[index], evidence, run, cache
            ):
                return None
            trusted.extend((row, rows[index]))
            index += 1
            if index < len(rows) and re.fullmatch(r"[ \t]+\^+", rows[index]):
                index += 1
            while index < len(rows) and (not rows[index] or re.match(r"E[ \t]", rows[index])):
                trusted.append(rows[index])
                index += 1
            if index < len(rows) and rows[index].startswith("        "):
                # Short locals have no trailing frame location. Keep the proven
                # prefix and never resume at markers that a multiline repr can forge.
                return "\n".join(trusted)
            continue
        arguments: list[str] = []
        if _metadata_row(row):
            while index < len(rows) and (not rows[index] or _metadata_row(rows[index])):
                if rows[index]:
                    arguments.append(rows[index])
                index += 1
            # Blanks are repr data, never proof that funcargs ended. Admit only
            # a def anchored to this section and its confirmed historical frame.
            if index == len(rows) or not re.match(r"    (?:async )?def \w+\(", rows[index]):
                return None
            row = rows[index]
        if _DEFINITION.match(row):
            definition_index = index
            while definition_index < len(rows) and rows[definition_index].startswith("    @"):
                definition_index += 1
            definition = (
                re.match(r"    (?:async )?def (\w+)\(", rows[definition_index])
                if definition_index < len(rows)
                else None
            )
            if header_name is None or definition is None:
                return None
            if first_definition and definition[1] != header_name:
                return None
            frame = _long_frame(rows, index, arguments, evidence, run, cache)
            if frame is None:
                return None
            index, source = frame
            trusted.extend(source)
            first_definition = False
            continue
        if _ASSIGNMENT.search(row) or _LOCATION.fullmatch(row):
            return None
        trusted.append(row)
        index += 1
    return "\n".join(trusted)


def terminal_runtime_frame(section: str, original_tail: str) -> str | None:
    """Reject missing, conflicting or ambiguous original exception locations."""
    exception = r"[A-Za-z_][\w.]*"
    detail_pattern = re.compile(rf"E[ \t]+(?P<exception>{exception})(?::|[ \t]*$)")
    location_pattern = re.compile(r":\d+:")
    location_detail_pattern = re.compile(rf"[ \t]+(?P<exception>{exception})(?::.*)?$")
    error_header_pattern = re.compile(r"^_+[ \t]+ERROR\b.*[ \t]+_+[ \t]*$")

    def exception_location(row: str) -> str | None:
        if row.startswith(("E ", "E\t")):
            return None
        # Split first, so a message cannot make matching retry a later delimiter.
        location = location_pattern.search(row)
        if location is None or not row[: location.start()].strip():
            return None
        detail = location_detail_pattern.fullmatch(row[location.end() :])
        return detail["exception"] if detail is not None else None

    original_rows = original_tail.splitlines()
    original_exceptions = {
        location for row in original_rows if (location := exception_location(row)) is not None
    }
    # Headers are never endpoints: a repr can forge them. Any later header makes
    # attribution ambiguous, even when the later failure has the same exception.
    if (
        original_exceptions != {"NotImplementedError"}
        or any(
            not row.startswith(("E ", "E\t"))
            and exception_location(row) is None
            and len(location_pattern.findall(row)) > 1
            for row in original_rows
        )
        or any(error_header_pattern.match(row) for row in original_rows)
        or any(
            _PYTEST_FAILURE_HEADER_RE.match(row) or _RTK_FAILURE_HEADER_RE.match(row)
            for row in original_rows[1:]
        )
    ):
        return None
    rows = section.splitlines()
    frame_start = 0
    terminal_start: int | None = None
    terminal_exception: str | None = None
    location_exception: str | None = None
    for index, row in enumerate(rows):
        if _SHORT.fullmatch(row) or _DEFINITION.match(row):
            frame_start = index
        detail = detail_pattern.match(row)
        if detail is not None:
            terminal_start = frame_start
            terminal_exception = detail["exception"]
        location = exception_location(row)
        if location is not None:
            location_exception = location
    if (
        terminal_start is None
        or terminal_exception != "NotImplementedError"
        or (location_exception or terminal_exception) != terminal_exception
    ):
        return None
    return "\n".join(rows[terminal_start:])
