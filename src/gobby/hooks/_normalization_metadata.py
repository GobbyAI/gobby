"""Assemble canonical semantics and scope from classified shell segments."""

from collections.abc import Mapping
from typing import Any

from gobby.hooks._normalization_bindings import (
    _loop_binding_variable_is_stable,
    _loop_header_words_are_literal,
    _plain_loop_binding_reference,
    _shell_loop_binding_disqualifications,
    _shell_segment_preserves_loop_binding,
)
from gobby.hooks._normalization_segments import _ShellSegmentMetadata
from gobby.hooks._normalization_shell import _contains_unexpanded_shell_reference


def _build_canonical_tool_metadata(
    kind: str,
    *,
    paths: list[str] | None = None,
    write_paths: list[str] | None = None,
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
    if write_paths:
        data["canonical_write_file_paths"] = write_paths
        data["canonical_write_file_path"] = write_paths[0]
    if repo_mutation:
        data["canonical_repo_mutation"] = True
    if extra:
        data.update(extra)
    return data


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
        search_extras = [
            extra for extra in extras if extra.get("canonical_code_navigation_action") == "search"
        ]
        if search_extras and all(
            extra.get("canonical_search_revision_scoped") for extra in search_extras
        ):
            merged["canonical_search_revision_scoped"] = True
        else:
            merged.pop("canonical_search_revision_scoped", None)
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
    write_paths: list[str] = []
    mutation_scope_unknown = False
    mutation_scope_resolved_by_loop_binding = True
    saw_unexpanded_mutation_path = False
    navigation_scope_unknown = False
    loop_bindings: dict[str, tuple[str, ...]] = {}
    disqualified_loop_variables = set().union(
        *(_shell_loop_binding_disqualifications(item.shell_words) for item in metadata)
    )
    for item in metadata:
        command_is_known = bool(
            item.kind != "execute"
            or item.repo_mutation
            or item.neutral_setup
            or item.read_only_pipeline_filter
            or (item.extra and item.extra.get("canonical_code_navigation_action"))
        )
        for bound_variable in tuple(loop_bindings):
            if not _shell_segment_preserves_loop_binding(
                item.shell_words,
                bound_variable,
                command_is_known=command_is_known,
            ):
                loop_bindings.pop(bound_variable)
        if item.loop_binding_variable:
            if (
                item.loop_binding_variable not in disqualified_loop_variables
                and item.paths
                and _loop_header_words_are_literal(item.shell_words, item.shell_raw_words)
            ):
                loop_bindings[item.loop_binding_variable] = item.paths
            else:
                loop_bindings.pop(item.loop_binding_variable, None)
        for variable, value in item.assignment_bindings:
            if variable not in disqualified_loop_variables and _loop_binding_variable_is_stable(
                variable
            ):
                loop_bindings[variable] = (value,)
        resolvable = [path for path in item.paths if not _contains_unexpanded_shell_reference(path)]
        if (
            item.extra
            and item.extra.get("canonical_code_navigation_action")
            and len(resolvable) != len(item.paths)
        ):
            navigation_scope_unknown = True
        unresolved_mutation_paths = (
            [path for path in item.paths if _contains_unexpanded_shell_reference(path)]
            if item.repo_mutation
            else []
        )
        if unresolved_mutation_paths:
            mutation_scope_unknown = True
            saw_unexpanded_mutation_path = True
            for path in unresolved_mutation_paths:
                reference = _plain_loop_binding_reference(
                    path,
                    item.shell_words,
                    item.shell_raw_words,
                )
                if reference and reference[0] in loop_bindings:
                    variable, suffix = reference
                    for bound_path in loop_bindings[variable]:
                        resolved_path = bound_path + suffix
                        if resolved_path not in mutation_paths:
                            mutation_paths.append(resolved_path)
                else:
                    mutation_scope_resolved_by_loop_binding = False
        for path in resolvable:
            if path not in paths:
                paths.append(path)
            if item.repo_mutation and path not in mutation_paths:
                mutation_paths.append(path)
        for path in item.write_paths:
            if not _contains_unexpanded_shell_reference(path) and path not in write_paths:
                write_paths.append(path)

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
    extra["canonical_code_navigation_segments"] = [
        {**dict(item.extra), "canonical_file_paths": list(item.paths)}
        for item in active
        if item.extra and item.extra.get("canonical_code_navigation_action")
    ]
    if not pure_gcode_navigation:
        extra = _without_code_index_navigation(extra)
    if mutation_scope_unknown:
        extra["_canonical_repo_mutation_scope_unknown"] = True
    if saw_unexpanded_mutation_path and mutation_scope_resolved_by_loop_binding:
        extra["_canonical_repo_mutation_scope_resolved_by_loop_binding"] = True
    if navigation_scope_unknown and not paths:
        extra["_canonical_code_navigation_scope_unknown"] = True

    # Only publish paths proved to belong to mutating segments. A live loop
    # binding promotes its header paths into that set when the body references
    # the bound variable. An empty mutation set is not a licence to relax:
    # `paths_may_touch_project` treats it as unknown scope.
    effective_paths = mutation_paths if kind == "write" else paths

    return _build_canonical_tool_metadata(
        kind,
        paths=effective_paths or None,
        write_paths=write_paths or None,
        repo_mutation=any(item.repo_mutation for item in active),
        confidence="low" if any(item.confidence == "low" for item in active) else "high",
        extra=extra or None,
    )
