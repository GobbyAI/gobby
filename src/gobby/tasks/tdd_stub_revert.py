"""Stub-revert proof: a RED caused by a stub that one exact inverse edit removes."""

from __future__ import annotations

import re

from gobby.tasks.tdd_paths import is_production_edit_path
from gobby.tasks.tdd_python_evidence import _source_confirmed_before
from gobby.tasks.transcript_evidence_models import (
    TranscriptEdit,
    TranscriptEvidence,
    TranscriptValidationRun,
)

_RAISES_NOT_IMPLEMENTED_RE = re.compile(r"\braise\s+NotImplementedError\b")


def has_stub_revert_proof(
    evidence: TranscriptEvidence,
    red: TranscriptValidationRun,
    revert: TranscriptEdit | None,
) -> bool:
    """Credit a RED that only the stub separates from a GREEN of the same command.

    The edit just before ``red`` must stub production code with ``raise
    NotImplementedError``, and ``revert``, the edit closing the RED window, must swap
    exactly that text back. The first later run of the RED's command must pass, and
    no other edit may fall between the stub and that pass. This credits tests that
    reach the stub only indirectly, such as through an HTTP client.
    """
    if revert is None:
        return False
    edits = sorted(evidence.edits, key=lambda edit: edit.order)
    stub = next((edit for edit in reversed(edits) if edit.order < red.order), None)
    if (
        stub is None
        or stub.session_id != red.session_id
        or stub.path != revert.path
        or not is_production_edit_path(stub.path)
        or not _source_confirmed_before(stub, red)
        or not _is_exact_inverse(stub, revert)
    ):
        return False
    green = next(
        (
            run
            for run in sorted(evidence.validation_runs, key=lambda item: item.order)
            if run.order > revert.order and run.core_command == red.core_command
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
    """The stub adds ``raise NotImplementedError``; the revert swaps the same text back."""
    added, removed = stub.source_fragment, stub.source_replaced
    return (
        added is not None
        and removed is not None
        and _RAISES_NOT_IMPLEMENTED_RE.search(added) is not None
        and _RAISES_NOT_IMPLEMENTED_RE.search(removed) is None
        and revert.source_fragment == removed
        and revert.source_replaced == added
    )
