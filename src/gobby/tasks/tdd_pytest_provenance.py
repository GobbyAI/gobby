"""Verify assignment-bearing pytest source displays before trusting runtime frames."""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import PurePosixPath

from gobby.tasks.tdd_compacted_output import _PYTEST_FAILURE_HEADER_RE
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


def terminal_runtime_frame(section: str) -> str | None:
    """Select only the terminal raising frame when its final exception agrees."""
    rows = section.splitlines()
    exception = r"(?:[A-Za-z_][\w.]*(?:Error|Exception)|Failed)"
    detail_pattern = re.compile(rf"E[ \t]+(?P<exception>{exception})(?::|[ \t]*$)")
    location_pattern = re.compile(rf"\S+\.py:\d+:[ \t]+(?P<exception>{exception})(?::.*)?$")
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
        location = location_pattern.fullmatch(row)
        if location is not None:
            location_exception = location["exception"]
    if (
        terminal_start is None
        or terminal_exception != "NotImplementedError"
        or (location_exception or terminal_exception) != terminal_exception
    ):
        return None
    return "\n".join(rows[terminal_start:])
