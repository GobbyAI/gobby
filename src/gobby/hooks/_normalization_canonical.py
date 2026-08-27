"""Canonical tool metadata inference."""

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gobby.hooks._normalization_operands import (
    _curl_output_paths,
    _git_add_positional_args_after,
    _search_command_paths,
    _truncate_positional_paths,
)
from gobby.hooks._normalization_paths import (
    _extract_tool_input_paths,
    _setdefault_tool_input_paths,
    extract_structured_mutation_paths,
)
from gobby.hooks._normalization_shell import (
    _SHELL_CHAIN_TOKENS,
    ShellToken,
    _get_command_text,
    _has_perl_inplace_option,
    _has_sed_inplace_option,
    _looks_file_like,
    _looks_path_target,
    _shell_positional_args,
    _strip_shell_wrappers,
    extract_redirection_paths,
    has_mutating_output_redirection,
    has_shell_input_redirection,
    is_shell_input_redirection_token,
    is_unquoted_shell_control_token,
    shell_token_values,
    strip_output_redirections,
    tokenize_shell_command,
)
from gobby.hooks._path_scope import apply_path_scope_metadata
from gobby.hooks._python_pipeline_classifier import (
    _classify_python_pipeline_with_targets,
    _classify_python_source_with_targets,
    _inline_interpreter_parts,
    _is_read_only_python_pipeline,
    _PythonExecutionClassification,
)
from gobby.hooks.code_navigation import (
    count_option_line_count,
    gcode_navigation_metadata,
    line_count_from_tool_input,
    search_navigation_metadata,
    sed_line_count,
    shell_command_name,
    source_read_navigation_metadata,
)

_CANONICAL_READ_TOOL_NAMES = frozenset({"read"})
CANONICAL_WRITE_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "applypatch",
        "create",
        "create_file",
        "createfile",
        "delete_file",
        "deletefile",
        "edit",
        "edit_file",
        "editfile",
        "move_file",
        "movefile",
        "notebook_edit",
        "notebookedit",
        "patch_file",
        "patchfile",
        "replace",
        "search_replace",
        "searchreplace",
        "write",
        "write_file",
        "writefile",
    }
)
_MCP_FILE_MUTATION_LEAF_TOOLS = frozenset(
    {
        "apply_patch",
        "create_file",
        "delete_file",
        "edit_file",
        "move_file",
        "patch_file",
        "replace_file",
        "write_file",
    }
)
_GCODE_PIPELINE_READ_ONLY_FILTERS = frozenset(
    {"cat", "cut", "grep", "head", "jq", "rg", "sed", "sort", "tail", "tr", "uniq", "wc"}
)
# Characters in echo arguments that imply command substitution rather than a plain marker.
_ECHO_UNSAFE_CHARS = frozenset({"$", "`"})

# `$` opening a variable (`$VAR`, `${VAR}`), command substitution (`$(cmd)`),
# positional parameter (`$1`), or special parameter — anything expanded at runtime.
_UNEXPANDED_SHELL_REFERENCE = re.compile(r"\$[\w{(@*?#$!-]")


@dataclass(frozen=True, slots=True)
class _ShellSegment:
    tokens: list[ShellToken]
    separator_before: str | None = None


@dataclass(frozen=True, slots=True)
class _ShellSegmentMetadata:
    kind: str
    paths: tuple[str, ...] = ()
    extra: Mapping[str, Any] | None = None
    repo_mutation: bool = False
    confidence: str = "high"
    neutral_setup: bool = False
    pure_gcode_navigation: bool = False
    read_only_pipeline_filter: bool = False
    # A python program fed via stdin (heredoc); the body may prove it read-only
    # or name the literal paths it writes, rebased against this segment's cwd.
    stdin_python_program: bool = False
    cwd: str | None = None


def _build_canonical_tool_metadata(
    kind: str,
    *,
    paths: list[str] | None = None,
    repo_mutation: bool = False,
    confidence: str = "high",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a canonical metadata payload for a tool event."""
    data: dict[str, Any] = {
        "canonical_tool_kind": kind,
        "canonical_tool_confidence": confidence,
    }
    if paths:
        data["canonical_file_paths"] = paths
        data["canonical_file_path"] = paths[0]
    if repo_mutation:
        data["canonical_repo_mutation"] = True
    if extra:
        data.update(extra)
    return data


def _compact_tool_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(character for character in value.casefold() if character.isalnum())


def _is_structured_file_mutation(data: Mapping[str, Any], tool_name: Any) -> bool:
    """Classify provider-native and known MCP file mutation tools."""
    tool_name_lower = tool_name.casefold() if isinstance(tool_name, str) else ""
    if (
        tool_name_lower in CANONICAL_WRITE_TOOL_NAMES
        or _compact_tool_name(tool_name) in CANONICAL_WRITE_TOOL_NAMES
    ):
        return True

    mcp_tool = data.get("mcp_tool")
    if not isinstance(mcp_tool, str):
        return False
    return mcp_tool.casefold() in _MCP_FILE_MUTATION_LEAF_TOOLS


def _is_read_only_pipeline_stage(tokens: list[ShellToken], parts: list[str]) -> bool:
    if not parts:
        return False
    if has_shell_input_redirection(tokens) or has_mutating_output_redirection(tokens):
        return False
    cmd = shell_command_name(parts[0])
    if cmd == "sed" and _has_sed_inplace_option(parts):
        return False
    return cmd in _GCODE_PIPELINE_READ_ONLY_FILTERS or _is_read_only_python_pipeline(parts)


def _stdin_program_is_python(parts: list[str]) -> bool:
    interpreter_parts = _inline_interpreter_parts(parts)
    if not interpreter_parts:
        return False
    return shell_command_name(interpreter_parts[0]) in {"python", "python3"}


def _interpreter_reads_program_from_stdin(parts: list[str]) -> bool:
    interpreter_parts = _inline_interpreter_parts(parts)
    if not interpreter_parts:
        return False
    interpreter = shell_command_name(interpreter_parts[0])
    if interpreter not in {"node", "python", "python3", "ruby"}:
        return False
    args = interpreter_parts[1:]
    if any(flag in args for flag in {"-c", "-e", "--eval", "-m"}):
        return False
    return "-" in args or not any(not arg.startswith("-") for arg in args)


def _is_neutral_echo_segment(tokens: list[ShellToken], parts: list[str]) -> bool:
    """Return True for side-effect-free ``echo`` segments used as output markers."""
    if not parts or shell_command_name(parts[0]) != "echo":
        return False
    if has_shell_input_redirection(tokens) or has_mutating_output_redirection(tokens):
        return False
    return not any(ch in part for part in parts[1:] for ch in _ECHO_UNSAFE_CHARS)


def _split_shell_segments(tokens: list[ShellToken]) -> list[_ShellSegment]:
    segments: list[_ShellSegment] = []
    current: list[ShellToken] = []
    separator_before: str | None = None
    for token in tokens:
        if not token.quoted and token.value in _SHELL_CHAIN_TOKENS:
            if current:
                segments.append(_ShellSegment(current, separator_before))
                current = []
            separator_before = token.value
            continue
        current.append(token)
    if current:
        segments.append(_ShellSegment(current, separator_before))
    return segments


def _literal_cd_target(parts: list[str]) -> str | None:
    if not parts or shell_command_name(parts[0]) != "cd":
        return None
    positional = [part for part in parts[1:] if part and not part.startswith("-")]
    if len(positional) != 1:
        return None
    target = positional[0]
    if any(char in target for char in "$`*?[]{};"):
        return None
    return target


def _rebase_shell_path(path: str, cwd: str | None) -> str:
    if not cwd or path.startswith(("/", "~")) or "://" in path:
        return path
    return posixpath.normpath(posixpath.join(cwd, path))


def _contains_unexpanded_shell_reference(path: str) -> bool:
    """Detect ``$VAR``, ``${VAR}``, ``$(cmd)``, and positional-parameter tokens.

    Shell-extracted path tokens keep their source text, so a token containing
    an unexpanded reference names an unknowable location; recording it verbatim
    fabricates attribution and rule-match paths.
    """
    return _UNEXPANDED_SHELL_REFERENCE.search(path) is not None


def _rebase_shell_paths(paths: list[str], cwd: str | None) -> list[str]:
    return [_rebase_shell_path(path, cwd) for path in paths]


def _apply_cd(cwd: str | None, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target)
    if not cwd:
        return posixpath.normpath(target)
    return posixpath.normpath(posixpath.join(cwd, target))


def _without_code_index_navigation(extra: Mapping[str, Any] | None) -> dict[str, Any]:
    if not extra:
        return {}
    data = dict(extra)
    data.pop("canonical_code_index_navigation", None)
    data.pop("canonical_code_index_command", None)
    return data


def _merge_code_navigation_extra(metadata: list[_ShellSegmentMetadata]) -> dict[str, Any]:
    extras = [dict(item.extra) for item in metadata if item.extra]
    if not extras:
        return {}

    merged: dict[str, Any] = {}
    for extra in extras:
        merged.update(extra)

    actions = [extra.get("canonical_code_navigation_action") for extra in extras]
    broad_values = [
        extra.get("canonical_code_navigation_broad")
        for extra in extras
        if "canonical_code_navigation_broad" in extra
    ]
    if "search" in actions:
        merged["canonical_code_navigation_action"] = "search"
        merged["canonical_code_navigation_broad"] = (
            any(bool(value) for value in broad_values) if broad_values else True
        )
    elif "read" in actions:
        merged["canonical_code_navigation_action"] = "read"
        if broad_values:
            merged["canonical_code_navigation_broad"] = any(bool(value) for value in broad_values)
    return merged


def _merge_shell_segment_metadata(metadata: list[_ShellSegmentMetadata]) -> dict[str, Any]:
    active = [
        item for item in metadata if not item.neutral_setup and not item.read_only_pipeline_filter
    ]
    if not active:
        return _build_canonical_tool_metadata("execute")

    paths: list[str] = []
    mutation_paths: list[str] = []
    mutation_scope_unknown = False
    for item in active:
        resolvable = [path for path in item.paths if not _contains_unexpanded_shell_reference(path)]
        if item.repo_mutation and any(
            _contains_unexpanded_shell_reference(path) for path in item.paths
        ):
            mutation_scope_unknown = True
        for path in resolvable:
            if path not in paths:
                paths.append(path)
            if item.repo_mutation and path not in mutation_paths:
                mutation_paths.append(path)

    pure_gcode_navigation = any(item.pure_gcode_navigation for item in metadata) and all(
        item.neutral_setup or item.pure_gcode_navigation or item.read_only_pipeline_filter
        for item in metadata
    )

    if any(item.kind == "write" for item in active):
        kind = "write"
    elif any(item.kind == "search" for item in active):
        kind = "search"
    elif any(item.kind == "read" for item in active):
        kind = "read"
    else:
        kind = "execute"

    extra = _merge_code_navigation_extra(active)
    if not pure_gcode_navigation:
        extra = _without_code_index_navigation(extra)
    if mutation_scope_unknown:
        extra["_canonical_repo_mutation_scope_unknown"] = True

    # A write command's paths are the ones it writes. Segments that only name
    # paths — a `for <var> in <words>` header, a read on the same line — are
    # scope evidence, and pooling them here attributed loop counters to tasks
    # and turned read-only probes into repo mutations. The header stays the
    # fallback when a mutating segment's own operands are unexpanded, which is
    # the only scope signal that case has. An empty mutation set is not a
    # licence to relax: `paths_may_touch_project` treats it as unknown scope.
    effective_paths = mutation_paths if kind == "write" and not mutation_scope_unknown else paths

    return _build_canonical_tool_metadata(
        kind,
        paths=effective_paths or None,
        repo_mutation=any(item.repo_mutation for item in active),
        confidence="low" if any(item.confidence == "low" for item in active) else "high",
        extra=extra or None,
    )


def _input_redirection_paths(tokens: list[ShellToken]) -> list[str]:
    paths: list[str] = []
    for idx, token in enumerate(tokens[:-1]):
        if not is_shell_input_redirection_token(token) or token.value != "<":
            continue
        candidate = tokens[idx + 1]
        if is_unquoted_shell_control_token(candidate):
            continue
        if _looks_path_target(candidate.value) and candidate.value not in paths:
            paths.append(candidate.value)
    return paths


def _normalize_shell_tool_metadata(command: str) -> dict[str, Any]:
    """Infer canonical semantics from visible shell command segments."""
    heredoc_bodies: list[str] = []
    try:
        tokens = tokenize_shell_command(command, heredoc_bodies=heredoc_bodies)
    except ValueError:
        return {}

    if not tokens:
        return {}

    persistent_cwd: str | None = None
    metadata: list[_ShellSegmentMetadata] = []
    segments = _split_shell_segments(tokens)
    for index, segment in enumerate(segments):
        in_pipeline = segment.separator_before == "|" or (
            index + 1 < len(segments) and segments[index + 1].separator_before == "|"
        )
        if segment.separator_before not in {None, "&&", ";", "\n", "|"}:
            persistent_cwd = None
        raw_parts = shell_token_values(segment.tokens)
        parts = _strip_shell_wrappers(raw_parts)
        if not parts:
            metadata.append(_ShellSegmentMetadata("execute", neutral_setup=True))
            continue

        cd_target = _literal_cd_target(parts)
        if cd_target is not None:
            if not in_pipeline and segment.separator_before in {None, "&&", ";", "\n"}:
                persistent_cwd = _apply_cd(persistent_cwd, cd_target)
            metadata.append(_ShellSegmentMetadata("execute", neutral_setup=True))
            continue

        if segment.separator_before == "|" and _is_read_only_pipeline_stage(segment.tokens, parts):
            metadata.append(
                _ShellSegmentMetadata(
                    "execute",
                    read_only_pipeline_filter=True,
                )
            )
            continue

        metadata.append(_classify_shell_segment(segment.tokens, parts, persistent_cwd))

    metadata = _classify_stdin_python(metadata, heredoc_bodies)
    return _merge_shell_segment_metadata(metadata)


def _classify_stdin_python(
    metadata: list[_ShellSegmentMetadata],
    heredoc_bodies: list[str],
) -> list[_ShellSegmentMetadata]:
    """Reclassify a lone Python heredoc from its body evidence.

    A proven mutation carries its literal targets as write paths; ambiguous
    shell shapes keep their conservative unscoped write classification.
    """
    flagged = [item for item in metadata if item.stdin_python_program]
    if len(flagged) != 1 or len(heredoc_bodies) != 1:
        return metadata
    classification, targets = _classify_python_source_with_targets(heredoc_bodies[0])
    if classification is _PythonExecutionClassification.MUTATION:
        replacement = _ShellSegmentMetadata(
            "write",
            paths=tuple(_rebase_shell_paths(list(targets), flagged[0].cwd)),
            repo_mutation=True,
        )
    else:
        replacement = _ShellSegmentMetadata(
            "execute",
            confidence=(
                "high" if classification is _PythonExecutionClassification.READ_ONLY else "low"
            ),
        )
    return [replacement if item.stdin_python_program else item for item in metadata]


def _classify_shell_segment(
    tokens: list[ShellToken],
    parts: list[str],
    cwd: str | None,
) -> _ShellSegmentMetadata:
    redirection_paths = _rebase_shell_paths(extract_redirection_paths(tokens), cwd)
    input_paths = _rebase_shell_paths(_input_redirection_paths(tokens), cwd)

    # Classify the base command without redirection operators/targets so a
    # redirect target never masquerades as a positional file argument.
    plain_tokens = strip_output_redirections(tokens)
    plain_parts = (
        parts
        if len(plain_tokens) == len(tokens)
        else _strip_shell_wrappers(shell_token_values(plain_tokens))
    )

    gcode_metadata = gcode_navigation_metadata(plain_parts)
    if gcode_metadata and not (
        has_shell_input_redirection(tokens) or has_mutating_output_redirection(tokens)
    ):
        kind, extra = gcode_metadata
        gcode_paths = _rebase_shell_paths(
            [path for path in plain_parts[2:] if _looks_file_like(path)],
            cwd,
        )
        return _ShellSegmentMetadata(
            kind,
            paths=tuple(gcode_paths),
            extra=extra,
            pure_gcode_navigation=True,
        )

    if redirection_paths:
        base_metadata = _classify_shell_segment_without_redirection(plain_parts, cwd)
        extra = _without_code_index_navigation(base_metadata.extra)
        base_paths = list(base_metadata.paths)
        return _ShellSegmentMetadata(
            "write",
            paths=tuple(
                base_paths + [path for path in redirection_paths if path not in base_paths]
            ),
            extra=extra,
            repo_mutation=True,
        )

    if input_paths:
        base_metadata = _classify_shell_segment_without_redirection(plain_parts, cwd)
        if _interpreter_reads_program_from_stdin(plain_parts):
            base_metadata = _ShellSegmentMetadata(
                "write",
                repo_mutation=True,
                stdin_python_program=_stdin_program_is_python(plain_parts),
                cwd=cwd,
            )
        base_paths = list(base_metadata.paths)
        if base_metadata.repo_mutation and not base_paths:
            input_paths = []
        return _ShellSegmentMetadata(
            base_metadata.kind,
            paths=tuple(base_paths + [path for path in input_paths if path not in base_paths]),
            extra=base_metadata.extra,
            repo_mutation=base_metadata.repo_mutation,
            neutral_setup=base_metadata.neutral_setup,
            pure_gcode_navigation=base_metadata.pure_gcode_navigation,
            read_only_pipeline_filter=base_metadata.read_only_pipeline_filter,
        )

    if has_shell_input_redirection(tokens):
        if _interpreter_reads_program_from_stdin(plain_parts):
            return _ShellSegmentMetadata(
                "write",
                repo_mutation=True,
                stdin_python_program=_stdin_program_is_python(plain_parts),
                cwd=cwd,
            )
        return _ShellSegmentMetadata("execute")

    if _is_neutral_echo_segment(tokens, plain_parts):
        return _ShellSegmentMetadata("execute", neutral_setup=True)

    return _classify_shell_segment_without_redirection(plain_parts, cwd)


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
    items = [part for part in parts[in_index + 1 :] if _looks_path_target(part)]
    return _ShellSegmentMetadata(
        "execute",
        paths=tuple(_rebase_shell_paths(items, cwd)),
    )


def _classify_shell_segment_without_redirection(
    parts: list[str],
    cwd: str | None,
) -> _ShellSegmentMetadata:
    if not parts:
        return _ShellSegmentMetadata("execute")

    cmd = shell_command_name(parts[0])

    if cmd == "for":
        return _classify_for_loop_header(parts, cwd)

    git_subcommand_index = 1
    if cmd == "git":
        while git_subcommand_index < len(parts):
            part = parts[git_subcommand_index]
            if part in {"-C", "-c"}:
                git_subcommand_index += 2
                continue
            if part.startswith("-"):
                git_subcommand_index += 1
                continue
            break
    if cmd == "git" and parts[git_subcommand_index : git_subcommand_index + 1] == ["apply"]:
        apply_args = parts[git_subcommand_index + 1 :]
        if any(flag in apply_args for flag in {"--check", "--stat", "--numstat"}):
            return _ShellSegmentMetadata("execute")
        return _ShellSegmentMetadata("write", repo_mutation=True)

    if cmd == "git" and parts[git_subcommand_index : git_subcommand_index + 1] == ["add"]:
        positional = _git_add_positional_args_after(parts, git_subcommand_index + 1)
        paths = [
            candidate
            for candidate in positional
            if _looks_path_target(candidate) or _contains_unexpanded_shell_reference(candidate)
        ]
        return _ShellSegmentMetadata(
            "execute",
            paths=tuple(_rebase_shell_paths(paths, cwd)),
            repo_mutation=True,
        )

    if cmd == "git" and parts[git_subcommand_index : git_subcommand_index + 1] in [
        ["checkout"],
        ["restore"],
    ]:
        try:
            pathspec_index = parts.index("--", git_subcommand_index + 1)
        except ValueError:
            paths = []
        else:
            paths = [
                candidate
                for candidate in parts[pathspec_index + 1 :]
                if _looks_path_target(candidate) or _contains_unexpanded_shell_reference(candidate)
            ]
        return _ShellSegmentMetadata(
            "write",
            paths=tuple(_rebase_shell_paths(paths, cwd)),
            repo_mutation=True,
        )

    if cmd == "git" and parts[git_subcommand_index : git_subcommand_index + 1] == ["revert"]:
        return _ShellSegmentMetadata("write", repo_mutation=True)

    if cmd == "patch":
        if "--dry-run" in parts[1:]:
            return _ShellSegmentMetadata("execute")
        return _ShellSegmentMetadata("write", repo_mutation=True)

    interpreter_parts = _inline_interpreter_parts(parts)
    if interpreter_parts:
        interpreter = shell_command_name(interpreter_parts[0])
        interpreter_args = interpreter_parts[1:]
        if interpreter in {"python", "python3"} and "-c" in interpreter_args:
            classification, targets = _classify_python_pipeline_with_targets(parts)
            if classification is _PythonExecutionClassification.READ_ONLY:
                return _ShellSegmentMetadata("execute")
            if classification is _PythonExecutionClassification.INDETERMINATE:
                return _ShellSegmentMetadata("execute", confidence="low")
            return _ShellSegmentMetadata(
                "write",
                paths=tuple(_rebase_shell_paths(list(targets), cwd)),
                repo_mutation=True,
            )
        if (
            interpreter == "node" and any(flag in interpreter_args for flag in {"-e", "--eval"})
        ) or (interpreter == "ruby" and "-e" in interpreter_args):
            return _ShellSegmentMetadata("write", repo_mutation=True)

    if cmd == "curl":
        writes_file, paths = _curl_output_paths(parts)
        if writes_file:
            return _ShellSegmentMetadata(
                "write",
                paths=tuple(_rebase_shell_paths(paths, cwd)),
                repo_mutation=True,
            )
        return _ShellSegmentMetadata("execute")

    if cmd in {"rg", "grep", "git", "find"}:
        if cmd == "git" and (len(parts) <= 1 or parts[1] != "grep"):
            return _ShellSegmentMetadata("execute")
        paths = _rebase_shell_paths(_search_command_paths(cmd, parts), cwd)
        return _ShellSegmentMetadata(
            "search",
            paths=tuple(paths),
            extra=search_navigation_metadata(paths),
        )

    if cmd in {"cat", "head", "tail", "bat", "nl"}:
        positional = _shell_positional_args(parts)
        paths = _rebase_shell_paths(
            [candidate for candidate in positional if _looks_file_like(candidate)],
            cwd,
        )
        line_count = count_option_line_count(parts) if cmd in {"head", "tail"} else None
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

    if cmd == "sed" and len(parts) >= 2:
        positional = _shell_positional_args(parts)
        candidate = positional[-1] if positional else None
        if _has_sed_inplace_option(parts):
            paths = [candidate] if candidate and _looks_path_target(candidate) else []
            return _ShellSegmentMetadata(
                "write",
                paths=tuple(_rebase_shell_paths(paths, cwd)),
                repo_mutation=True,
            )
        paths = _rebase_shell_paths(
            [item for item in positional if _looks_file_like(item)],
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
        return _ShellSegmentMetadata(
            "write",
            paths=tuple(_rebase_shell_paths(paths, cwd)),
            repo_mutation=True,
        )

    if cmd == "tee":
        positional = _shell_positional_args(parts)
        paths = [candidate for candidate in positional if _looks_path_target(candidate)]
        return _ShellSegmentMetadata(
            "write",
            paths=tuple(_rebase_shell_paths(paths, cwd)),
            repo_mutation=True,
        )

    if cmd in {"touch", "rm", "mkdir", "rmdir"}:
        positional = _shell_positional_args(parts)
        paths = [candidate for candidate in positional if _looks_path_target(candidate)]
        return _ShellSegmentMetadata(
            "write",
            paths=tuple(_rebase_shell_paths(paths, cwd)),
            repo_mutation=True,
        )

    if cmd in {"cp", "mv", "install"}:
        positional = _shell_positional_args(parts)
        candidate = positional[-1] if positional else None
        paths = [candidate] if candidate and _looks_path_target(candidate) else []
        return _ShellSegmentMetadata(
            "write",
            paths=tuple(_rebase_shell_paths(paths, cwd)),
            repo_mutation=True,
        )

    if cmd == "truncate":
        paths = _rebase_shell_paths(_truncate_positional_paths(parts), cwd)
        return _ShellSegmentMetadata(
            "write",
            paths=tuple(paths),
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
        metadata = _build_canonical_tool_metadata(
            "write",
            paths=canonical_paths,
            repo_mutation=True,
        )
        metadata["canonical_structured_mutation"] = True
        metadata["canonical_file_paths"] = canonical_paths
    elif tool_name_lower in {"grep_search", "grep"}:
        metadata = _build_canonical_tool_metadata("search")
    elif tool_name == "Bash":
        command = _get_command_text(tool_input)
        if command:
            metadata = _normalize_shell_tool_metadata(command)
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

    if canonical_file_paths:
        _setdefault_tool_input_paths(tool_input, canonical_file_paths)

    data.update(metadata)
