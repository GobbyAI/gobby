"""Assemble canonical semantics and scope from classified shell segments."""

from collections.abc import Mapping
from dataclasses import replace
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
from gobby.hooks._sed_program import sed_write_paths
from gobby.hooks.code_navigation import source_read_navigation_metadata


def _unknown_write_metadata() -> dict[str, Any]:
    """Fail closed when shell parsing cannot resolve a mutation's scope."""
    return _build_canonical_tool_metadata(
        "write",
        repo_mutation=True,
        confidence="low",
        extra={"canonical_repo_mutation_scope_unknown": True},
    )


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


def _gate_rewritable_scripts(
    metadata: list[_ShellSegmentMetadata],
) -> list[_ShellSegmentMetadata]:
    """Make classified script bodies opaque when another segment of the command writes.

    A body is classified as it reads now. Any other write in the command, before it,
    beside it in a pipeline or on a later loop pass, may rewrite it first.
    """
    writes = [
        item.kind == "write" or item.repo_mutation or bool(item.write_paths) for item in metadata
    ]
    return [
        replace(item, extra={**item.extra, "canonical_script_execution": True})
        if item.extra
        and item.extra.get("canonical_script_bodies")
        and any(write for other, write in enumerate(writes) if other != index)
        else item
        for index, item in enumerate(metadata)
    ]


def _path_is_unresolved(path: str, root: str | None) -> bool:
    if root and path == root:
        return False
    if root:
        path = path.removeprefix(root.rstrip("/") + "/")
    return _contains_unexpanded_shell_reference(path)


def _loop_bound_sed_program_is_inert(
    item: _ShellSegmentMetadata, loop_bindings: dict[str, tuple[str, ...]]
) -> bool:
    program = item.extra.get("_canonical_sed_bsd_program") if item.extra else None
    if not isinstance(program, str):
        return False
    reference = _plain_loop_binding_reference(program, item.shell_words, item.shell_raw_words)
    if reference is None or reference[0] not in loop_bindings:
        return False
    variable, suffix = reference
    return all(
        sed_write_paths(bound + suffix, bsd=True) == ([], False)
        for bound in loop_bindings[variable]
    )


def _merge_shell_segment_metadata(metadata: list[_ShellSegmentMetadata]) -> dict[str, Any]:
    active = [
        item for item in metadata if not item.neutral_setup and not item.read_only_pipeline_filter
    ]
    if not active:
        return _build_canonical_tool_metadata("execute")

    paths: list[str] = []
    mutation_paths: list[str] = []
    edit_paths: list[str] = []
    write_paths: list[str] = []
    mutation_scope_unknown = False
    mutation_scope_resolved_by_loop_binding = True
    saw_unexpanded_mutation_path = False
    navigation_scope_unknown = False
    navigation_scope_paths: list[str] = []
    navigation_segments: list[dict[str, Any]] = []
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
        resolvable = [
            path for path in item.paths if not _path_is_unresolved(path, item.literal_path_root)
        ]
        if item.neutral_setup or item.read_only_pipeline_filter:
            pass
        elif item.extra and item.extra.get("canonical_code_navigation_segments"):
            navigation_segments.extend(
                dict(segment) for segment in item.extra["canonical_code_navigation_segments"]
            )
        elif item.extra and (
            item.extra.get("canonical_code_navigation_action")
            or item.extra.get("_canonical_deferred_source_read")
        ):
            navigation_paths: list[str] = []
            for path in item.navigation_paths or item.paths:
                reference = _plain_loop_binding_reference(
                    path, item.shell_words, item.shell_raw_words
                )
                if reference and reference[0] in loop_bindings:
                    variable, suffix = reference
                    navigation_paths.extend(value + suffix for value in loop_bindings[variable])
                else:
                    navigation_paths.append(path)
            navigation_extra = dict(item.extra)
            if navigation_extra.pop("_canonical_deferred_source_read", False):
                navigation_extra = source_read_navigation_metadata(
                    navigation_paths,
                    line_count=navigation_extra.get("canonical_source_line_count"),
                    byte_count=navigation_extra.get("canonical_source_byte_count"),
                    read_scope=navigation_extra["canonical_source_read_scope"],
                )
            if navigation_extra.get("canonical_code_navigation_action"):
                navigation_segments.append(
                    {**navigation_extra, "canonical_file_paths": navigation_paths}
                )
        if item.extra and item.extra.get("canonical_code_navigation_action"):
            for path in item.extra.get("_canonical_code_navigation_scope_paths", ()):
                if _path_is_unresolved(path, item.literal_path_root):
                    navigation_scope_unknown = True
                elif path not in navigation_scope_paths:
                    navigation_scope_paths.append(path)
            for path in item.navigation_paths or item.paths:
                if not _path_is_unresolved(path, item.literal_path_root):
                    continue
                reference = _plain_loop_binding_reference(
                    path, item.shell_words, item.shell_raw_words
                )
                if reference and reference[0] in loop_bindings:
                    variable, suffix = reference
                    for bound_path in loop_bindings[variable]:
                        resolved_path = bound_path + suffix
                        if resolved_path not in paths:
                            paths.append(resolved_path)
                else:
                    navigation_scope_unknown = True
        unresolved_mutation_paths = (
            [path for path in item.paths if _path_is_unresolved(path, item.literal_path_root)]
            if item.repo_mutation
            else []
        )
        if item.repo_mutation and (
            (item.kind == "write" and not item.paths)
            or (
                item.extra
                and item.extra.get("_canonical_repo_mutation_scope_unknown")
                and not item.extra.get("_canonical_repo_mutation_scope_resolved_by_loop_binding")
                and not _loop_bound_sed_program_is_inert(item, loop_bindings)
            )
        ):
            # A sibling's literal paths cannot resolve this segment's opaque write.
            mutation_scope_unknown = True
            mutation_scope_resolved_by_loop_binding = False
        explicit_edit_paths = item.extra.get("canonical_edit_file_paths") if item.extra else None
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
                        if (
                            explicit_edit_paths is None or resolved_path in explicit_edit_paths
                        ) and resolved_path not in edit_paths:
                            edit_paths.append(resolved_path)
                else:
                    mutation_scope_resolved_by_loop_binding = False
        for path in resolvable:
            if path not in paths:
                paths.append(path)
            if item.repo_mutation and path not in mutation_paths:
                mutation_paths.append(path)
            if (
                item.repo_mutation
                and (explicit_edit_paths is None or path in explicit_edit_paths)
                and path not in edit_paths
            ):
                edit_paths.append(path)
        for path in item.write_paths:
            if not _path_is_unresolved(path, item.literal_path_root) and path not in write_paths:
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
    if "canonical_edit_file_paths" in extra:
        # Scope-only segments authorize the command without claiming a directory.
        # Preserve actual edits in mixed commands and nested shell wrappers.
        extra["canonical_edit_file_paths"] = edit_paths
    extra.pop("_canonical_deferred_source_read", None)
    extra["canonical_code_navigation_segments"] = navigation_segments
    if not pure_gcode_navigation:
        extra = _without_code_index_navigation(extra)
    # Every level's classified script bodies, which a single extra would overwrite.
    script_bodies = {
        body
        for item in active
        if item.extra
        for body in item.extra.get("canonical_script_bodies", ())
    }
    if script_bodies:
        extra["canonical_script_bodies"] = sorted(script_bodies)
    if mutation_scope_unknown and kind == "write":
        extra["_canonical_repo_mutation_scope_unknown"] = True
    if any(item.unverified_write_scope for item in active):
        extra["_canonical_repo_mutation_scope_unverified"] = True
    if saw_unexpanded_mutation_path and mutation_scope_resolved_by_loop_binding:
        extra["_canonical_repo_mutation_scope_resolved_by_loop_binding"] = True
    elif mutation_scope_unknown:
        extra.pop("_canonical_repo_mutation_scope_resolved_by_loop_binding", None)
    if navigation_scope_paths:
        extra["_canonical_code_navigation_scope_paths"] = navigation_scope_paths
    else:
        extra.pop("_canonical_code_navigation_scope_paths", None)
    if navigation_scope_unknown:
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
