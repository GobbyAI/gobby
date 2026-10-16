"""Classify plain shell commands independently of recursive shell execution."""

from pathlib import Path

from gobby.hooks._inline_interpreter_classifier import (
    _classify_inline_interpreter,
    _InlineProgramClassification,
)
from gobby.hooks._normalization_bindings import _loop_binding_variable_is_stable
from gobby.hooks._normalization_file_commands import classify_file_command
from gobby.hooks._normalization_git import classify_git_mutation, classify_search_segment
from gobby.hooks._normalization_interpreter_segments import _python_write_segment
from gobby.hooks._normalization_operands import (
    _curl_output_paths,
    _find_has_mutation_predicate,
    _search_command_paths,
)
from gobby.hooks._normalization_sed import classify_sed_segment
from gobby.hooks._normalization_segments import _ShellSegmentMetadata
from gobby.hooks._normalization_shell import (
    _contains_unexpanded_shell_reference,
    _looks_file_like,
    _looks_path_target,
    _rebase_navigation_shell_paths,
    _rebase_shell_paths,
    _shell_positional_args,
)
from gobby.hooks._python_pipeline_classifier import (
    _classify_python_pipeline_evidence,
    _inline_interpreter_parts,
    _PythonExecutionClassification,
)
from gobby.hooks._shell_execution_words import _unwrap
from gobby.hooks.code_navigation import (
    count_option_byte_count,
    count_option_line_count,
    find_navigation_metadata,
    shell_command_name,
    source_read_navigation_metadata,
)


def classify_base_command(
    parts: list[str],
    cwd: str | None,
    *,
    shell_stdin: bool = False,
    base_cwd: Path | None = None,
    raw_parts: tuple[str, ...] | None = None,
) -> _ShellSegmentMetadata:
    if not parts:
        return _ShellSegmentMetadata("execute")
    cmd = shell_command_name(parts[0])

    if cmd == "for":
        return _classify_for_loop_header(parts, cwd)

    if cmd == "git":
        git_metadata = classify_git_mutation(parts, cwd, base_cwd)
        if git_metadata is not None:
            return git_metadata

    if cmd == "patch":
        if "--dry-run" in parts[1:]:
            return _ShellSegmentMetadata("execute")
        return _ShellSegmentMetadata("write", repo_mutation=True)

    interpreter_parts = _inline_interpreter_parts(parts)
    if interpreter_parts:
        interpreter = shell_command_name(interpreter_parts[0])
        interpreter_args = interpreter_parts[1:]
        if interpreter in {"python", "python3"} and "-c" in interpreter_args:
            python_classification, mutation = _classify_python_pipeline_evidence(parts)
            if python_classification is _PythonExecutionClassification.READ_ONLY:
                return _ShellSegmentMetadata("execute")
            if python_classification is _PythonExecutionClassification.INDETERMINATE:
                return _ShellSegmentMetadata("execute", confidence="low")
            return _python_write_segment(mutation, cwd)
        has_inline_program = (interpreter == "ruby" and "-e" in interpreter_args) or (
            interpreter == "node"
            and any(
                argument in {"-e", "--eval"} or argument.startswith("--eval=")
                for argument in interpreter_args
            )
        )
        if has_inline_program:
            inline_classification = _classify_inline_interpreter(interpreter, interpreter_args)
            if inline_classification is _InlineProgramClassification.MUTATION:
                return _ShellSegmentMetadata("write", repo_mutation=True)
            return _ShellSegmentMetadata(
                "execute",
                confidence=(
                    "high"
                    if inline_classification is _InlineProgramClassification.READ_ONLY
                    else "low"
                ),
            )

    if cmd == "curl":
        writes_file, paths = _curl_output_paths(parts)
        if writes_file:
            rebased_paths = tuple(_rebase_shell_paths(paths, cwd))
            return _ShellSegmentMetadata(
                "write",
                paths=rebased_paths,
                write_paths=rebased_paths,
                repo_mutation=True,
            )
        return _ShellSegmentMetadata("execute")

    if cmd == "find":
        paths = _rebase_navigation_shell_paths(_search_command_paths(cmd, parts), cwd)
        return _ShellSegmentMetadata(
            "execute",
            paths=tuple(paths),
            extra=find_navigation_metadata(parts, paths),
            repo_mutation=_find_has_mutation_predicate(parts),
        )

    if cmd in {"rg", "grep", "git"}:
        return classify_search_segment(cmd, parts, cwd, base_cwd, shell_stdin=shell_stdin)

    if cmd == "xargs" and _unwrap(parts)[:1] == ["cat"]:
        # Stdin supplies file operands, not source contents; their scope is unknown.
        navigation = {
            "canonical_code_navigation_action": "read",
            "canonical_code_navigation_broad": True,
            "canonical_source_read_scope": "full_file",
            "_canonical_code_navigation_scope_unknown": True,
        }
        return _ShellSegmentMetadata(
            "read",
            extra={**navigation, "canonical_code_navigation_segments": [navigation]},
        )

    if cmd == "awk" and parts[1:2] == ["1"]:
        # The constant-true program prints every input record.
        parts = ["cat", *parts[2:]]
        cmd = "cat"

    if cmd in {"cat", "head", "tail", "bat", "nl", "less"}:
        positional = _shell_positional_args(parts)
        paths = _rebase_navigation_shell_paths(
            [
                candidate
                for candidate in positional
                if _looks_file_like(candidate) or _contains_unexpanded_shell_reference(candidate)
            ],
            cwd,
        )
        line_count = count_option_line_count(parts) if cmd in {"head", "tail"} else None
        byte_count = count_option_byte_count(parts) if cmd in {"head", "tail"} else None
        read_scope = (
            "line_range"
            if line_count is not None
            else "byte_range"
            if byte_count is not None
            else "full_file"
        )
        return _ShellSegmentMetadata(
            "read",
            paths=tuple(paths),
            extra=source_read_navigation_metadata(
                paths,
                line_count=line_count,
                byte_count=byte_count,
                read_scope=read_scope,
                defer_unresolved=True,
            ),
        )

    if cmd == "sed" and len(parts) >= 2:
        return classify_sed_segment(parts, cwd, raw_parts, base_cwd)

    return classify_file_command(cmd, parts, cwd, base_cwd)


def _classify_for_loop_header(parts: list[str], cwd: str | None) -> _ShellSegmentMetadata:
    """Surface literal iteration paths from a ``for <var> in ...`` header.

    The loop body arrives as separate segments whose operands are unexpanded
    variables (dropped as path evidence), so the header's literal list is the
    only scope signal a ``for f in <paths>; do grep ... "$f"`` command has.
    """
    try:
        in_index = parts.index("in")
    except ValueError:
        return _ShellSegmentMetadata("execute")
    variable = parts[1] if len(parts) > 1 else ""
    items = parts[in_index + 1 :]
    if (
        in_index != 2
        or not _loop_binding_variable_is_stable(variable)
        or not items
        or any(not _looks_path_target(part) for part in items)
    ):
        return _ShellSegmentMetadata("execute")
    return _ShellSegmentMetadata(
        "execute",
        paths=tuple(_rebase_shell_paths(items, cwd)),
        loop_binding_variable=variable,
    )
