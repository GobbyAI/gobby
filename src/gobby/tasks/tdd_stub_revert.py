"""Stub-revert proof: a RED caused by a stub that one exact inverse edit removes."""

from __future__ import annotations

import ast
import textwrap

from gobby.tasks.tdd_paths import is_production_edit_path
from gobby.tasks.tdd_python_evidence import _source_confirmed_before
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)


def has_stub_revert_proof(
    evidence: TranscriptEvidence,
    red: TranscriptValidationRun,
    revert: TranscriptEdit | None,
) -> bool:
    """Credit a RED that only the stub separates from a GREEN of the same command.

    The edit just before ``red`` must stub production code with an executable ``raise
    NotImplementedError``, and ``revert``, the edit closing the RED window, must swap
    exactly that text back and restore the file as it was before the stub. The first
    later run of the RED's command in the same session must pass, and no other edit
    may fall between the stub and that pass. Stub, RED, revert and GREEN all belong
    to one session. This credits tests that reach the stub only indirectly, such as
    through an HTTP client.
    """
    if revert is None:
        return False
    edits = sorted(evidence.edits, key=lambda edit: edit.order)
    stub = next((edit for edit in reversed(edits) if edit.order < red.order), None)
    if (
        stub is None
        or stub.session_id != red.session_id
        or revert.session_id != red.session_id
        or stub.path != revert.path
        or not is_production_edit_path(stub.path)
        or not _source_confirmed_before(stub, red)
        or not _is_exact_inverse(stub, revert)
    ):
        return False
    before_stub = next(
        (edit for edit in reversed(edits) if edit.order < stub.order and edit.path == stub.path),
        None,
    )
    if (
        before_stub is None
        or before_stub.source_after is None
        or revert.source_after != before_stub.source_after
    ):
        return False
    green = next(
        (
            run
            for run in sorted(evidence.validation_runs, key=lambda item: item.order)
            if run.order > revert.order
            and run.session_id == red.session_id
            and run.core_command == red.core_command
        ),
        None,
    )
    return (
        green is not None
        and green.outcome == "success"
        and _source_confirmed_before(revert, green)
        and not any(
            stub.order < edit.order < green.order and edit.order != revert.order for edit in edits
        )
    )


def _is_exact_inverse(stub: TranscriptEdit, revert: TranscriptEdit) -> bool:
    """The stub adds an executable ``raise NotImplementedError``; the revert swaps it back."""
    added, removed = stub.source_fragment, stub.source_replaced
    return (
        added is not None
        and removed is not None
        and _raises_not_implemented(added) is True
        and _raises_not_implemented(removed) is False
        and revert.source_fragment == removed
        and revert.source_replaced == added
    )


def _raises_not_implemented(fragment: str) -> bool | None:
    """Whether a parsed fragment raises ``NotImplementedError``; None when it does not parse.

    Parsing ignores the marker inside comments and strings.
    """
    try:
        module = ast.parse(textwrap.dedent(fragment))
    except SyntaxError:
        return None
    for node in ast.walk(module):
        if isinstance(node, ast.Raise):
            error = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(error, ast.Name) and error.id == "NotImplementedError":
                return True
    return False
