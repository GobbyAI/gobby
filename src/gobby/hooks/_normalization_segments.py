"""Shell segment records and the output bounds a pipeline places on them."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from gobby.hooks._normalization_shell import (
    _SHELL_CHAIN_TOKENS,
    ShellToken,
    _shell_positional_args,
)
from gobby.hooks.code_navigation import (
    MAX_NARROW_SOURCE_LINES,
    count_option_line_count,
    sed_line_count,
    shell_command_name,
)


@dataclass(frozen=True, slots=True)
class _ShellSegment:
    tokens: list[ShellToken]
    separator_before: str | None = None


@dataclass(frozen=True, slots=True)
class _ShellSegmentMetadata:
    kind: str
    paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    extra: Mapping[str, Any] | None = None
    repo_mutation: bool = False
    confidence: str = "high"
    neutral_setup: bool = False
    pure_gcode_navigation: bool = False
    read_only_pipeline_filter: bool = False
    # An interpreter program fed via stdin (heredoc); its body may prove it
    # read-only or mutating. Python also reports literal write targets.
    stdin_program_interpreter: str | None = None
    # Literal write targets that surrounding untrusted interpreter code could
    # redirect; they resolve the write's scope only inside the project.
    unverified_write_scope: bool = False
    cwd: str | None = None
    loop_binding_variable: str | None = None
    # Absolute literal `name=/path` bindings from a leading bare-assignment segment.
    assignment_bindings: tuple[tuple[str, str], ...] = ()
    shell_words: tuple[str, ...] = ()
    shell_raw_words: tuple[str, ...] = ()
    # A trusted tool cwd is already literal; only the rebased operand expands.
    literal_path_root: str | None = None
    # Read operands of a segment classified as a write; navigation judges these.
    navigation_paths: tuple[str, ...] = ()


def _split_shell_segments(tokens: list[ShellToken]) -> list[_ShellSegment]:
    """Split tokenized shell input at unquoted chain operators."""
    segments: list[_ShellSegment] = []
    current: list[ShellToken] = []
    separator_before: str | None = None
    for token in tokens:
        if not token.quoted and token.value in _SHELL_CHAIN_TOKENS:
            # Preserve pipeline, conditional and background operators across trailing newlines.
            if not current and token.value == "\n" and separator_before in {"|", "&&", "||", "&"}:
                continue
            if current:
                segments.append(_ShellSegment(current, separator_before))
                current = []
            separator_before = token.value
            continue
        current.append(token)
    if current:
        segments.append(_ShellSegment(current, separator_before))
    return segments


def _pipeline_filter_output_line_bound(parts: list[str]) -> int | None:
    """Verified output bound, in lines, of a read-only pipeline filter stage."""
    command = shell_command_name(parts[0])
    if command in {"head", "tail"}:
        return count_option_line_count(parts)
    if command == "sed":
        return sed_line_count(parts, _shell_positional_args(parts))
    return None


def _bound_pipeline_reads(
    metadata: list[_ShellSegmentMetadata],
    segments: list[_ShellSegment],
    filter_index: int,
    bound: int | None,
) -> None:
    """Narrow reads whose pipeline ends in a filter with a verified output bound.

    The content a bounded final stage releases is all the model sees, so an
    upstream broad window no longer describes the read.
    """
    if bound is None or bound > MAX_NARROW_SOURCE_LINES:
        return
    for prior in range(filter_index - 1, -1, -1):
        if segments[prior + 1].separator_before != "|":
            break
        item = metadata[prior]
        if not item.extra:
            continue
        deferred = item.extra.get("_canonical_deferred_source_read")
        navigation = deferred if deferred else item.extra
        pending = item.extra.get("_canonical_pending_source_reads")
        if (
            navigation.get("canonical_code_navigation_action") == "read"
            and navigation.get("canonical_code_navigation_broad")
            or pending
        ):
            narrowed = {
                "canonical_code_navigation_broad": False,
                "canonical_narrow_source_context": True,
                "canonical_source_line_count": bound,
            }
            extra = dict(item.extra)
            if deferred:
                extra["_canonical_deferred_source_read"] = {**deferred, **narrowed}
            elif navigation.get("canonical_code_navigation_action") == "read":
                extra.update(narrowed)
            if pending:
                extra["_canonical_pending_source_reads"] = [
                    {**record, "scope": {**record["scope"], **narrowed}} for record in pending
                ]
            if "canonical_code_navigation_segments" in extra:
                extra["canonical_code_navigation_segments"] = [
                    {**segment, **narrowed}
                    if segment.get("canonical_code_navigation_action") == "read"
                    else segment
                    for segment in extra["canonical_code_navigation_segments"]
                ]
            metadata[prior] = replace(
                item,
                extra=extra,
            )
