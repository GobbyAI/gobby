"""Translate interpreter source evidence into shell mutation metadata."""

from gobby.hooks._inline_interpreter_classifier import (
    _classify_interpreter_source,
    _InlineProgramClassification,
)
from gobby.hooks._normalization_segments import _ShellSegmentMetadata
from gobby.hooks._normalization_shell import _rebase_shell_paths
from gobby.hooks._python_pipeline_classifier import (
    _classify_python_source_evidence,
    _PythonExecutionClassification,
    _PythonMutationEvidence,
)


def _classify_stdin_python(
    metadata: list[_ShellSegmentMetadata],
    heredoc_bodies: list[str],
) -> list[_ShellSegmentMetadata]:
    """Reclassify a lone interpreter heredoc from its body evidence.

    Python mutations carry literal targets as write paths. Ambiguous shell
    shapes keep their conservative unscoped write classification.
    """
    flagged = [item for item in metadata if item.stdin_program_interpreter]
    if len(flagged) != 1 or len(heredoc_bodies) != 1:
        return metadata
    interpreter = flagged[0].stdin_program_interpreter
    mutation: _PythonMutationEvidence | None = None
    if interpreter in {"python", "python3"}:
        python_classification, mutation = _classify_python_source_evidence(heredoc_bodies[0])
        is_mutation = python_classification is _PythonExecutionClassification.MUTATION
        is_read_only = python_classification is _PythonExecutionClassification.READ_ONLY
    else:
        inline_classification = _classify_interpreter_source(interpreter or "", heredoc_bodies[0])
        is_mutation = inline_classification is _InlineProgramClassification.MUTATION
        is_read_only = inline_classification is _InlineProgramClassification.READ_ONLY
    if is_mutation:
        replacement = _python_write_segment(mutation, flagged[0].cwd)
    else:
        replacement = _ShellSegmentMetadata(
            "execute",
            confidence="high" if is_read_only else "low",
        )
    return [replacement if item.stdin_program_interpreter else item for item in metadata]


def _python_write_segment(
    mutation: _PythonMutationEvidence | None, cwd: str | None
) -> _ShellSegmentMetadata:
    """A proven interpreter write carrying its literal targets, rebased onto ``cwd``."""
    targets = tuple(_rebase_shell_paths(list(mutation.targets or ()), cwd)) if mutation else ()
    return _ShellSegmentMetadata(
        "write",
        paths=targets,
        write_paths=targets,
        repo_mutation=True,
        unverified_write_scope=mutation is not None and mutation.unverified,
    )
