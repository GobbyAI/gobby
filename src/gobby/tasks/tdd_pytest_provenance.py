"""Verify assignment-bearing pytest source displays before trusting runtime frames."""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import PurePosixPath

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


def has_unverified_pytest_assignments(
    output: str,
    evidence: TranscriptEvidence,
    run: TranscriptValidationRun,
    cache: PythonModuleCache | None = None,
) -> bool:
    """Reject locals before their repr can introduce headers or source/frame markers.

    Only positioned, contiguous source spans from confirmed pre-run snapshots exempt
    assignment separators. Read forward: a later forged block cannot exempt a prior row.
    The caller applies the first-Captured cutoff before passing the retained prefix.
    """
    rows = output.splitlines()
    verified: set[int] = set()
    for index, row in enumerate(rows):
        if _ASSIGNMENT.search(row) and index not in verified and not _DEFINITION.match(row):
            return True
        short = _SHORT.fullmatch(row)
        if short is not None and index + 1 < len(rows):
            if _ASSIGNMENT.search(rows[index + 1]):
                if not _short_source_matches(short, rows[index + 1], evidence, run, cache):
                    return True
                verified.add(index + 1)
            continue
        if not _DEFINITION.match(row):
            continue
        end = index + 1
        while end < len(rows) and rows[end].startswith(("    ", ">   ")):
            end += 1
        assignments = {offset for offset in range(index, end) if _ASSIGNMENT.search(rows[offset])}
        if not assignments:
            continue
        located = end
        # Source ends before exception detail. Never consume locals as source or skip
        # them while looking for the location that terminates a long frame.
        while located < len(rows) and (not rows[located] or re.match(r"E[ \t]", rows[located])):
            if _ASSIGNMENT.search(rows[located]):
                return True
            located += 1
        location = _LOCATION.fullmatch(rows[located]) if located < len(rows) else None
        if location is None or not _long_source_matches(
            rows, index, end, location, evidence, run, cache
        ):
            return True
        verified.update(assignments)
    return False
