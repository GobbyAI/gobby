"""Canonical tool metadata inference."""

from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any

from gobby.hooks._inline_interpreter_classifier import (
    _classify_inline_interpreter,
    _classify_interpreter_source,
    _InlineProgramClassification,
)
from gobby.hooks._normalization_bindings import (
    _literal_assignment_bindings,
    _loop_binding_variable_is_stable,
    _raw_shell_word_is_literal,
)
from gobby.hooks._normalization_compound import (
    case_pattern_process_bodies,
    command_substitution_bodies,
    heredoc_substitution_bodies,
    process_substitution_bodies,
)
from gobby.hooks._normalization_cwd import ShellDirectoryState
from gobby.hooks._normalization_env import (
    UNRESOLVED_DIRECTORY,
    UNRESOLVED_WRAPPER,
    env_directory_operands,
)
from gobby.hooks._normalization_git import (
    classify_git_mutation,
    classify_search_segment,
    exported_git_selector,
    git_environment_parts,
    git_selector_setup,
)
from gobby.hooks._normalization_metadata import (
    _build_canonical_tool_metadata,
    _gate_rewritable_scripts,
    _merge_shell_segment_metadata,
    _without_code_index_navigation,
)
from gobby.hooks._normalization_operands import (
    _curl_output_paths,
    _file_command_operands,
    _file_transfer_paths,
    _find_has_mutation_predicate,
    _search_command_paths,
    _truncate_positional_paths,
)
from gobby.hooks._normalization_paths import (
    CANONICAL_WRITE_TOOL_NAMES as CANONICAL_WRITE_TOOL_NAMES,
)
from gobby.hooks._normalization_paths import (
    _compact_tool_name as _compact_tool_name,
)
from gobby.hooks._normalization_paths import (
    _extract_tool_input_paths,
    _is_structured_file_mutation,
    _setdefault_tool_input_paths,
    _structured_write_paths,
    extract_structured_mutation_paths,
)
from gobby.hooks._normalization_pipeline import (
    _is_neutral_echo_segment,
    _is_read_only_pipeline_stage,
)
from gobby.hooks._normalization_segments import (
    _bound_pipeline_reads,
    _pipeline_filter_output_line_bound,
    _ShellSegmentMetadata,
    _split_shell_segments,
)
from gobby.hooks._normalization_shell import (
    ShellToken,
    _apply_cd,
    _contains_unexpanded_shell_reference,
    _get_command_text,
    _has_perl_inplace_option,
    _has_sed_inplace_option,
    _input_redirection_paths,
    _literal_cd_target,
    _looks_file_like,
    _looks_path_target,
    _rebase_navigation_shell_paths,
    _rebase_shell_paths,
    _shell_positional_args,
    _strip_shell_wrappers,
    extract_redirection_paths,
    has_mutating_output_redirection,
    has_shell_input_redirection,
    redirects_stdout_to_file,
    scan_shell_command,
    shell_token_values,
    strip_input_redirections,
    strip_output_redirections,
)
from gobby.hooks._path_scope import (
    apply_path_scope_metadata,
    current_tool_cwd,
    resolve_tool_path,
)
from gobby.hooks._python_pipeline_classifier import (
    _classify_python_pipeline_evidence,
    _classify_python_source_evidence,
    _inline_interpreter_parts,
    _PythonExecutionClassification,
    _PythonMutationEvidence,
)
from gobby.hooks.code_navigation import (
    count_option_byte_count,
    count_option_line_count,
    find_navigation_metadata,
    gcode_navigation_metadata,
    line_count_from_tool_input,
    search_navigation_metadata,
    sed_line_count,
    shell_command_name,
    source_read_navigation_metadata,
)
from gobby.hooks.code_navigation_recovery import (
    annotate_navigation,
    gcode_targets,
)
from gobby.hooks.provider_launch_guard import _unwrap
from gobby.hooks.shell_execution import (
    SCRIPT_READ_BUDGET,
    SHELL_WRAPPER_DEPTH,
    path_execution,
    preserves_directory,
    read_script_body,
    shell_execution,
    starts_elsewhere,
)

_CANONICAL_READ_TOOL_NAMES = frozenset({"read"})
# Script bodies the current top-level command may still read; none outside one.
_script_reads_left: ContextVar[int] = ContextVar("_script_reads_left", default=0)


def _stdin_program_interpreter(parts: list[str]) -> str | None:
    interpreter_parts = _inline_interpreter_parts(parts)
    if not interpreter_parts:
        return None
    interpreter = shell_command_name(interpreter_parts[0])
    return interpreter if interpreter in {"node", "python", "python3", "ruby"} else None


def _interpreter_reads_program_from_stdin(parts: list[str]) -> bool:
    interpreter_parts = _inline_interpreter_parts(parts)
    if not interpreter_parts:
        return False
    interpreter = shell_command_name(interpreter_parts[0])
    if interpreter not in {"node", "python", "python3", "ruby"}:
        return False
    args = interpreter_parts[1:]
    if any(flag in {"-c", "-e", "--eval", "-m"} or flag.startswith("--eval=") for flag in args):
        return False
    return "-" in args or not any(not arg.startswith("-") for arg in args)


def _normalize_shell_tool_metadata(
    command: str, *, cwd: str | None = None, depth: int = 0, base_cwd: Path | None = None
) -> dict[str, Any]:
    """Infer canonical semantics from visible shell command segments.

    ``base_cwd`` resolves relative command words only until a segment may change the
    shell's directory; after that they fail closed. A top-level call grants the
    script-read budget its nested calls share.
    """
    selectors = exported_git_selector.set(exported_git_selector.get() if depth else False)
    budget = _script_reads_left.set(SCRIPT_READ_BUDGET) if not depth else None
    entry = (exported_git_selector.get(), _script_reads_left.get())
    unstable: frozenset[int] = frozenset()
    try:
        while True:
            directory = ShellDirectoryState(cwd, unstable_loops=unstable)
            result = _classify_shell_command(
                command, directory=directory, depth=depth, base_cwd=base_cwd
            )
            if directory.altered_loops <= unstable:
                return result
            unstable |= directory.altered_loops
            exported_git_selector.set(entry[0])
            _script_reads_left.set(entry[1])
    finally:
        if budget is not None:
            _script_reads_left.reset(budget)
        exported_git_selector.reset(selectors)


def _classify_shell_command(
    command: str, *, directory: ShellDirectoryState, depth: int, base_cwd: Path | None
) -> dict[str, Any]:
    try:
        scan = scan_shell_command(command)
        masked_command, command_substitutions = command_substitution_bodies(
            command, scan.spans, depth=depth
        )
        if command_substitutions:
            scan = scan_shell_command(masked_command)
        unresolved_heredoc_positions: set[int] = set()
        for heredoc in scan.heredocs:
            if not heredoc.quoted:
                bodies, unresolved = heredoc_substitution_bodies(heredoc.text, depth=depth)
                position = scan.spans[heredoc.opener][0]
                command_substitutions.extend((position, substitution) for substitution in bodies)
                if unresolved:
                    unresolved_heredoc_positions.add(position)
    except ValueError:
        return _build_canonical_tool_metadata(
            "write",
            repo_mutation=True,
            confidence="low",
            extra={"canonical_repo_mutation_scope_unknown": True},
        )

    tokens = scan.tokens
    if not tokens:
        return {}

    heredoc_bodies = [body.text for body in scan.heredocs if body.terminated]
    raw_by_token = {
        id(token): command[start:end]
        for token, (start, end) in zip(scan.tokens, scan.spans, strict=True)
    }
    spans_by_token = dict(zip((id(token) for token in tokens), scan.spans, strict=True))
    case_depth = 0
    pending_case_pattern = False
    previous_end = 0
    metadata: list[_ShellSegmentMetadata] = []
    segments = _split_shell_segments(tokens)
    directory.prepare_groups(segments)
    # Only the bare assignments that open a command always run in this shell.
    leading_assignments = True
    for index, segment in enumerate(segments):
        consuming_case_pattern = pending_case_pattern
        start = spans_by_token[id(segment.tokens[0])][0]
        gap = command[previous_end:start]
        previous_end = spans_by_token[id(segment.tokens[-1])][1]
        # Case subjects and patterns can execute substitutions before arm syntax is stripped.
        segment_start = min(spans_by_token[id(token)][0] for token in segment.tokens)
        segment_end = max(spans_by_token[id(token)][1] for token in segment.tokens)
        _, process_bodies = process_substitution_bodies(segment.tokens, spans_by_token, command)
        arm_tokens, case_depth, pending_case_pattern = directory.compound_tokens(
            segment.tokens, gap, case_depth, pending_case_pattern
        )
        pattern_bodies, unreadable = case_pattern_process_bodies(
            segment.tokens, arm_tokens, spans_by_token, command
        )
        process_bodies.extend(pattern_bodies)
        if unreadable:
            metadata.append(
                _ShellSegmentMetadata(
                    "write",
                    repo_mutation=True,
                    extra={"_canonical_repo_mutation_scope_unknown": True},
                )
            )
        owner_tokens, _ = process_substitution_bodies(arm_tokens, spans_by_token, command)
        segment = replace(
            segment,
            tokens=owner_tokens,
            separator_before=(
                ";"
                if (consuming_case_pattern and not pending_case_pattern)
                or (case_depth and (";;" in gap or ";&" in gap))
                else segment.separator_before
            ),
        )
        next_separator = segments[index + 1].separator_before if index + 1 < len(segments) else None
        in_pipeline = directory.pipeline_stage(segment.separator_before, next_separator)
        directory.sequence_separator(segment.separator_before)
        raw_parts = shell_token_values(segment.tokens)
        source_parts = tuple(raw_by_token[id(token)] for token in segment.tokens)
        substitution_bodies = [
            body
            for position, body in command_substitutions
            if segment_start <= position < segment_end
        ]
        assignment_bindings = (
            _literal_assignment_bindings(tuple(raw_parts), source_parts)
            if leading_assignments
            and segment.separator_before in {None, "&&", ";", "\n"}
            and next_separator not in {"|", "&"}
            else None
        )
        leading_assignments = assignment_bindings is not None
        parts = _strip_shell_wrappers(raw_parts)
        if git_selector_setup(raw_parts, parts):
            exported_git_selector.set(True)
        env_directories = env_directory_operands(raw_parts)
        segment_cwd = directory.cwd
        if env_directories and segment_cwd is None and not directory.changed and base_cwd:
            segment_cwd = str(base_cwd)
        for operand in env_directories:
            segment_cwd = _apply_cd(segment_cwd, operand)
        if any(
            not _raw_shell_word_is_literal(word)
            for word in env_directory_operands(list(source_parts))
        ):
            segment_cwd = UNRESOLVED_DIRECTORY
        segment_base = None if directory.changed or starts_elsewhere(parts) else base_cwd
        if env_directories:
            segment_base = Path(segment_cwd) if segment_cwd else None
        for body in process_bodies:
            metadata.append(
                _classify_shell_segment_without_redirection(
                    ["sh", "-c", body],
                    directory.cwd,
                    depth=depth,
                    base_cwd=None if directory.changed else base_cwd,
                )
            )
        if any(
            segment_start <= position < segment_end for position in unresolved_heredoc_positions
        ):
            metadata.append(
                _ShellSegmentMetadata(
                    "write",
                    repo_mutation=True,
                    extra={"_canonical_repo_mutation_scope_unknown": True},
                )
            )
        for body in substitution_bodies:
            body_metadata = _classify_shell_segment_without_redirection(
                ["sh", "-c", body],
                directory.cwd,
                depth=depth,
                base_cwd=None if directory.changed else base_cwd,
            )
            if body_metadata.repo_mutation or (body_metadata.extra or {}).get(
                "canonical_script_execution"
            ):
                metadata.append(body_metadata)
        if parts[:1] != ["for"] and not preserves_directory(parts):
            directory.mark_unproven()
        if not parts:
            metadata.append(
                _ShellSegmentMetadata(
                    "execute",
                    neutral_setup=True,
                    assignment_bindings=assignment_bindings or (),
                    shell_words=tuple(raw_parts),
                    shell_raw_words=source_parts,
                )
            )
            continue

        cd_target = _literal_cd_target(parts)
        if parts[0].rsplit("/", 1)[-1] in {"cd", "pushd", "popd"}:
            if (
                not env_directories
                and not in_pipeline
                and segment.separator_before in {None, "&&", ";", "\n", "|", "||", "&"}
                and (
                    segment.separator_before not in {"||", "&"} or next_separator not in {"|", "&"}
                )
            ):
                directory.change(None if segment.separator_before in {"||", "&"} else cd_target)
            metadata.append(
                _ShellSegmentMetadata(
                    "execute",
                    neutral_setup=True,
                    shell_words=tuple(raw_parts),
                    shell_raw_words=source_parts,
                )
            )
            continue

        if segment.separator_before == "|" and _is_read_only_pipeline_stage(segment.tokens, parts):
            metadata.append(
                _ShellSegmentMetadata(
                    "execute",
                    read_only_pipeline_filter=True,
                    shell_words=tuple(raw_parts),
                    shell_raw_words=source_parts,
                )
            )
            if index + 1 >= len(segments) or segments[index + 1].separator_before != "|":
                _bound_pipeline_reads(
                    metadata, segments, index, _pipeline_filter_output_line_bound(parts)
                )
            continue

        metadata.append(
            replace(
                _classify_shell_segment(
                    segment.tokens,
                    parts,
                    directory.cwd,
                    depth=depth,
                    piped_stdin=segment.separator_before == "|",
                    base_cwd=segment_base,
                    invocation_cwd=segment_cwd,
                ),
                shell_words=tuple(raw_parts),
                shell_raw_words=source_parts,
                literal_path_root=(
                    str(base_cwd)
                    if base_cwd and all(_raw_shell_word_is_literal(word) for word in source_parts)
                    else None
                ),
            )
        )
        directory.apply_opaque_change(parts)

    metadata = _classify_stdin_python(metadata, heredoc_bodies)
    return _merge_shell_segment_metadata(_gate_rewritable_scripts(metadata))


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


def _classify_shell_segment(
    tokens: list[ShellToken],
    parts: list[str],
    cwd: str | None,
    *,
    depth: int = 0,
    piped_stdin: bool = False,
    base_cwd: Path | None = None,
    invocation_cwd: str | None = None,
) -> _ShellSegmentMetadata:
    redirection_paths = _rebase_shell_paths(extract_redirection_paths(tokens), cwd)
    input_paths = _rebase_navigation_shell_paths(_input_redirection_paths(tokens), cwd)
    cwd = invocation_cwd if invocation_cwd is not None else cwd

    # Classify the base command without redirection operators/targets so a
    # redirect target never masquerades as a positional file argument.
    plain_tokens = strip_output_redirections(tokens)
    plain_parts = (
        parts
        if len(plain_tokens) == len(tokens)
        else _strip_shell_wrappers(shell_token_values(plain_tokens))
    )
    # An interpreter with no script operand reads its program from stdin whether
    # or not a literal ``-`` is present, so decide that from parts with every
    # redirection removed: ``python3 <<EOF`` must classify like ``python3 - <<EOF``.
    stdin_parts = (
        plain_parts
        if not has_shell_input_redirection(tokens)
        else _strip_shell_wrappers(shell_token_values(strip_input_redirections(plain_tokens)))
    )
    shell_stdin = piped_stdin or has_shell_input_redirection(tokens)
    if plain_tokens and not plain_tokens[0].quoted and stdin_parts:
        body = stdin_parts[0]
        if body.startswith("(") and body.endswith(")") and not body.startswith("(("):
            stdin_parts = ["sh", "-c", body[1:-1]]
    stdin_parts = git_environment_parts(shell_token_values(tokens), stdin_parts)

    gcode_metadata = gcode_navigation_metadata(plain_parts)
    if gcode_metadata and not (
        has_shell_input_redirection(tokens) or has_mutating_output_redirection(tokens)
    ):
        kind, extra = gcode_metadata
        gcode_paths = _rebase_navigation_shell_paths(
            gcode_targets(plain_parts, extra["canonical_code_index_command"]),
            cwd,
        )
        for index, part in enumerate(plain_parts):
            if part == "--project" and index + 1 < len(plain_parts):
                extra["canonical_code_index_project"] = plain_parts[index + 1]
            elif part.startswith("--project="):
                extra["canonical_code_index_project"] = part.split("=", 1)[1]
        return _ShellSegmentMetadata(
            kind,
            paths=tuple(gcode_paths),
            extra=extra,
            pure_gcode_navigation=True,
        )

    if redirection_paths:
        base_metadata = _classify_shell_segment_without_redirection(
            stdin_parts, cwd, depth=depth, shell_stdin=shell_stdin, base_cwd=base_cwd
        )
        extra = _without_code_index_navigation(base_metadata.extra)
        if base_metadata.repo_mutation and not base_metadata.paths:
            # A redirect names its own destination, never the opaque base write's.
            extra["_canonical_repo_mutation_scope_unknown"] = True
        if "canonical_edit_file_paths" in extra:
            extra["canonical_edit_file_paths"] = list(
                dict.fromkeys([*extra["canonical_edit_file_paths"], *redirection_paths])
            )
        if extra.get("canonical_code_navigation_action") == "read" and redirects_stdout_to_file(
            tokens
        ):
            # Fully redirected stdout never reaches the model: a copy or
            # concatenation, not source navigation gcode could serve.
            extra = {
                key: extra[key]
                for key in ("canonical_script_execution", "canonical_script_bodies")
                if key in extra
            }
        # A shell reads its script as it runs, so output appended to it becomes code.
        if any(
            _rewrites_script(
                {"canonical_write_file_paths": redirection_paths}, Path(body), cwd, base_cwd
            )
            for body in extra.get("canonical_script_bodies", ())
        ):
            extra["canonical_script_execution"] = True
        base_paths = list(base_metadata.paths)
        base_write_paths = list(base_metadata.write_paths)
        return _ShellSegmentMetadata(
            "write",
            paths=tuple(
                base_paths + [path for path in redirection_paths if path not in base_paths]
            ),
            write_paths=tuple(
                base_write_paths
                + [path for path in redirection_paths if path not in base_write_paths]
            ),
            extra=extra,
            repo_mutation=True,
            unverified_write_scope=base_metadata.unverified_write_scope,
        )

    if input_paths:
        # Input redirection operands are stdin, never positional arguments.
        base_metadata = _classify_shell_segment_without_redirection(
            stdin_parts, cwd, depth=depth, shell_stdin=shell_stdin, base_cwd=base_cwd
        )
        if _interpreter_reads_program_from_stdin(stdin_parts):
            base_metadata = _ShellSegmentMetadata(
                "write",
                repo_mutation=True,
                stdin_program_interpreter=_stdin_program_interpreter(stdin_parts),
                cwd=cwd,
            )
        base_paths = list(base_metadata.paths)
        if base_metadata.repo_mutation:
            # A mutating segment only reads its stdin source, so that path
            # must not widen the mutation scope.
            input_paths = []
        return _ShellSegmentMetadata(
            base_metadata.kind,
            paths=tuple(base_paths + [path for path in input_paths if path not in base_paths]),
            write_paths=base_metadata.write_paths,
            extra=base_metadata.extra,
            repo_mutation=base_metadata.repo_mutation,
            neutral_setup=base_metadata.neutral_setup,
            pure_gcode_navigation=base_metadata.pure_gcode_navigation,
            read_only_pipeline_filter=base_metadata.read_only_pipeline_filter,
        )

    if has_shell_input_redirection(tokens):
        if _interpreter_reads_program_from_stdin(stdin_parts):
            return _ShellSegmentMetadata(
                "write",
                repo_mutation=True,
                stdin_program_interpreter=_stdin_program_interpreter(stdin_parts),
                cwd=cwd,
            )
        # A heredoc only feeds stdin; the command still writes what it names.
        return _classify_shell_segment_without_redirection(
            stdin_parts, cwd, depth=depth, shell_stdin=shell_stdin, base_cwd=base_cwd
        )

    if _is_neutral_echo_segment(tokens, plain_parts):
        return _ShellSegmentMetadata("execute", neutral_setup=True)

    return _classify_shell_segment_without_redirection(
        stdin_parts, cwd, depth=depth, shell_stdin=shell_stdin, base_cwd=base_cwd
    )


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


def _rewrites_script(
    nested: dict[str, Any], script: Path, cwd: str | None, base_cwd: Path | None
) -> bool:
    """Whether a script body may write its own file, which the shell is still reading."""
    paths = list(nested.get("canonical_write_file_paths", ()))
    if nested.get("canonical_repo_mutation"):
        paths += nested.get("canonical_file_paths", ())
    base = base_cwd if cwd is None else Path(cwd) if Path(cwd).is_absolute() else None
    for path in paths:
        target = resolve_tool_path(path, base)
        if target is None or script.is_relative_to(target):
            return True
    return False


def _classify_shell_segment_without_redirection(
    parts: list[str],
    cwd: str | None,
    *,
    depth: int = 0,
    shell_stdin: bool = False,
    base_cwd: Path | None = None,
) -> _ShellSegmentMetadata:
    if not parts:
        return _ShellSegmentMetadata("execute")

    cmd = shell_command_name(parts[0])

    if parts[0] == UNRESOLVED_WRAPPER:
        return _ShellSegmentMetadata(
            "write", repo_mutation=True, extra={"_canonical_repo_mutation_scope_unknown": True}
        )

    if depth:
        unwrapped = _unwrap(parts)
        if unwrapped and _contains_unexpanded_shell_reference(unwrapped[0]):
            return _ShellSegmentMetadata(
                "execute", confidence="low", extra={"canonical_script_execution": True}
            )

    execution = path_execution(parts, base_cwd) or shell_execution(parts, stdin=shell_stdin)
    if execution is not None:
        command = execution.command
        script: tuple[Path, str] | None = None
        reads_left = _script_reads_left.get()
        if execution.script_word is not None and depth < SHELL_WRAPPER_DEPTH and reads_left:
            # A shell script's body classifies like a `-c` program run in this directory.
            _script_reads_left.set(reads_left - 1)
            script = read_script_body(execution.script_word, base_cwd)
            command = script[1] if script else None
        if (execution.script_file and script is None) or (
            command is not None and depth >= SHELL_WRAPPER_DEPTH
        ):
            return _ShellSegmentMetadata("execute", extra={"canonical_script_execution": True})
        if command is not None:
            nested = _normalize_shell_tool_metadata(
                command, cwd=cwd, depth=depth + 1, base_cwd=base_cwd
            )
            if script is not None:
                nested["canonical_script_bodies"] = sorted(
                    {str(script[0]), *nested.get("canonical_script_bodies", ())}
                )
                if _rewrites_script(nested, script[0], cwd, base_cwd):
                    nested["canonical_script_execution"] = True
            extra = {
                key: value
                for key, value in nested.items()
                if key
                not in {
                    "canonical_tool_kind",
                    "canonical_tool_confidence",
                    "canonical_repo_mutation",
                    "canonical_file_path",
                    "canonical_file_paths",
                    "canonical_write_file_path",
                    "canonical_write_file_paths",
                }
            }
            return _ShellSegmentMetadata(
                nested.get("canonical_tool_kind", "execute"),
                paths=tuple(nested.get("canonical_file_paths", ())),
                write_paths=tuple(nested.get("canonical_write_file_paths", ())),
                repo_mutation=bool(nested.get("canonical_repo_mutation")),
                confidence=nested.get("canonical_tool_confidence", "high"),
                extra=extra or None,
            )
        return _ShellSegmentMetadata("execute")

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
        return classify_search_segment(cmd, parts, cwd, base_cwd)

    if cmd in {"cat", "head", "tail", "bat", "nl"}:
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
            ),
        )

    if cmd == "sed" and len(parts) >= 2:
        positional = _shell_positional_args(parts)
        candidate = positional[-1] if positional else None
        if _has_sed_inplace_option(parts):
            paths = [candidate] if candidate and _looks_path_target(candidate) else []
            rebased_paths = tuple(_rebase_shell_paths(paths, cwd))
            return _ShellSegmentMetadata(
                "write",
                paths=rebased_paths,
                write_paths=rebased_paths,
                repo_mutation=True,
            )
        paths = _rebase_navigation_shell_paths(
            [
                item
                for item in positional
                if _looks_file_like(item) or _contains_unexpanded_shell_reference(item)
            ],
            cwd,
        )
        line_count = sed_line_count(parts, positional)
        read_scope = "line_range" if line_count is not None else "full_file"
        return _ShellSegmentMetadata(
            "read",
            paths=tuple(paths),
            extra=source_read_navigation_metadata(
                paths,
                line_count=line_count,
                read_scope=read_scope,
            ),
        )

    if cmd == "perl" and _has_perl_inplace_option(parts):
        positional = _shell_positional_args(parts)
        candidate = positional[-1] if positional else None
        paths = [candidate] if candidate and _looks_path_target(candidate) else []
        rebased_paths = tuple(_rebase_shell_paths(paths, cwd))
        return _ShellSegmentMetadata(
            "write",
            paths=rebased_paths,
            write_paths=rebased_paths,
            repo_mutation=True,
        )

    if cmd == "tee":
        positional = _shell_positional_args(parts)
        paths = [candidate for candidate in positional if _looks_path_target(candidate)]
        rebased_paths = tuple(_rebase_shell_paths(paths, cwd))
        return _ShellSegmentMetadata(
            "write",
            paths=rebased_paths,
            write_paths=rebased_paths,
            repo_mutation=True,
        )

    if cmd == "touch":
        operands, _, _ = _file_command_operands(cmd, parts)
        paths = [candidate for candidate in operands if _looks_path_target(candidate)]
        rebased_paths = tuple(_rebase_shell_paths(paths, cwd))
        return _ShellSegmentMetadata(
            "write",
            paths=rebased_paths,
            write_paths=rebased_paths,
            repo_mutation=True,
        )

    if cmd in {"rm", "mkdir", "rmdir"}:
        operands, _, _ = _file_command_operands(cmd, parts)
        paths = [candidate for candidate in operands if _looks_path_target(candidate)]
        return _ShellSegmentMetadata(
            "write",
            paths=tuple(_rebase_shell_paths(paths, cwd)),
            repo_mutation=True,
        )

    if cmd in {"cp", "mv", "install"}:
        rebased_paths = tuple(_file_transfer_paths(cmd, parts, cwd, base_cwd))
        return _ShellSegmentMetadata(
            "write",
            paths=rebased_paths,
            write_paths=rebased_paths if cmd != "mv" else (),
            repo_mutation=True,
        )

    if cmd == "truncate":
        paths = _rebase_shell_paths(_truncate_positional_paths(parts), cwd)
        return _ShellSegmentMetadata(
            "write",
            paths=tuple(paths),
            write_paths=tuple(paths),
            repo_mutation=True,
        )

    return _ShellSegmentMetadata("execute")


def _set_canonical_tool_metadata(data: dict[str, Any]) -> None:
    """Annotate events with canonical read/search/write semantics across CLIs."""
    tool_name = data.get("tool_name")
    tool_input = data.get("tool_input")

    metadata: dict[str, Any] = {}
    tool_name_lower = tool_name.lower() if isinstance(tool_name, str) else ""

    is_structured_mutation = _is_structured_file_mutation(data, tool_name)
    if tool_name_lower in _CANONICAL_READ_TOOL_NAMES:
        metadata = _build_canonical_tool_metadata("read")
    elif is_structured_mutation:
        canonical_paths = extract_structured_mutation_paths(data)
        write_paths = _structured_write_paths(data, tool_name, canonical_paths)
        metadata = _build_canonical_tool_metadata(
            "write",
            paths=canonical_paths,
            write_paths=write_paths or None,
            repo_mutation=True,
        )
        metadata["canonical_structured_mutation"] = True
        metadata["canonical_file_paths"] = canonical_paths
    elif tool_name_lower in {"grep_search", "grep"}:
        metadata = _build_canonical_tool_metadata("search")
    elif tool_name == "Bash":
        command = _get_command_text(tool_input)
        if command:
            metadata = _normalize_shell_tool_metadata(command, base_cwd=current_tool_cwd(data))
    elif "mcp_server" in data and "mcp_tool" in data:
        metadata = _build_canonical_tool_metadata("mcp")

    canonical_file_paths = metadata.get("canonical_file_paths")
    if not isinstance(canonical_file_paths, list):
        canonical_file_paths = []

    if not canonical_file_paths:
        canonical_file_paths = _extract_tool_input_paths(tool_input)
        if canonical_file_paths:
            metadata["canonical_file_paths"] = canonical_file_paths

    if canonical_file_paths and "canonical_file_path" not in metadata:
        metadata["canonical_file_path"] = canonical_file_paths[0]

    if (
        metadata.get("canonical_tool_kind") == "read"
        and "canonical_code_navigation_broad" not in metadata
    ):
        line_count = line_count_from_tool_input(tool_input)
        read_scope = "line_range" if line_count is not None else "full_file"
        metadata.update(
            source_read_navigation_metadata(
                canonical_file_paths,
                line_count=line_count,
                read_scope=read_scope,
            )
        )

    if (
        metadata.get("canonical_tool_kind") == "search"
        and not metadata.get("canonical_code_index_navigation")
        and "canonical_code_navigation_broad" not in metadata
    ):
        metadata.update(search_navigation_metadata(canonical_file_paths))

    apply_path_scope_metadata(data, metadata, canonical_file_paths)
    annotate_navigation(data, metadata)

    if canonical_file_paths:
        _setdefault_tool_input_paths(tool_input, canonical_file_paths)

    data.update(metadata)
