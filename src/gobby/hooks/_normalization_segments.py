"""Shell segment records and the output bounds a pipeline places on them."""

from __future__ import annotations

import re
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
    stderr_pipe: bool = False


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
    stderr_pipe = False
    for index, token in enumerate(tokens):
        if token.value == "&" and not token.quoted and 0 < index < len(tokens) - 1:
            previous, following = tokens[index - 1], tokens[index + 1]
            if (
                not previous.quoted
                and re.fullmatch(r"\d*(?:>>|[<>])", previous.value)
                and re.fullmatch(r"\d+|-", following.value)
            ):
                current.append(token)
                continue
        if not token.quoted and token.value in _SHELL_CHAIN_TOKENS:
            # Preserve pipeline, conditional and background operators across trailing newlines.
            if not current and token.value == "\n" and separator_before in {"|", "&&", "||", "&"}:
                continue
            if current:
                segments.append(_ShellSegment(current, separator_before, stderr_pipe))
                current = []
            # Pipeline bookkeeping treats both forms alike. Keep the stderr
            # distinction for consumers that prove the literal stdin program.
            stderr_pipe = token.value == "|&"
            separator_before = "|" if stderr_pipe else token.value
            continue
        current.append(token)
    if current:
        segments.append(_ShellSegment(current, separator_before, stderr_pipe))
    return segments


def _pipeline_filter_output_line_bound(parts: list[str]) -> int | None:
    """Verified output bound, in lines, of a read-only pipeline filter stage."""
    command = shell_command_name(parts[0])
    if command in {"head", "tail"}:
        return count_option_line_count(parts)
    if command == "sed":
        return sed_line_count(parts, _shell_positional_args(parts))
    return None


def _compound_stage_start(segments: list[_ShellSegment], end: int) -> int:
    """Find the opener of a compound stage ending immediately before a pipe."""
    closers = {"done": {"for", "select", "while", "until"}, "fi": {"if"}, "}": {"{"}}
    pending: list[str] = []
    for index in range(end, -1, -1):
        prefixes: list[str] = []
        for token in segments[index].tokens:
            if token.quoted or token.value not in {
                *closers,
                "for",
                "select",
                "while",
                "until",
                "if",
                "{",
                "do",
                "then",
                "else",
            }:
                break
            prefixes.append(token.value)
        for word in reversed(prefixes):
            if word in closers:
                pending.append(word)
            elif pending and word in closers[pending[-1]]:
                pending.pop()
        if not pending:
            return index
    return end


def _pipeline_read_indices(segments: list[_ShellSegment], index: int) -> list[int]:
    prior = index - 1
    indices: list[int] = []
    while prior >= 0:
        if segments[prior + 1].separator_before != "|":
            break
        start = _compound_stage_start(segments, prior)
        indices.extend(range(start, prior + 1))
        prior = start - 1
    return indices


def _bound_pipeline_reads(
    metadata: list[_ShellSegmentMetadata],
    segments: list[_ShellSegment],
    segment_metadata_positions: list[int],
    filter_index: int,
    bound: int | None,
) -> None:
    """Narrow reads whose pipeline ends in a filter with a verified output bound.

    The content a bounded final stage releases is all the model sees, so an
    upstream broad window no longer describes the read.
    """
    if bound is None or bound > MAX_NARROW_SOURCE_LINES:
        return
    for prior in _pipeline_read_indices(segments, filter_index):
        prior = segment_metadata_positions[prior]
        item = metadata[prior]
        if not item.extra:
            continue
        deferred = item.extra.get("_canonical_deferred_source_read")
        navigation = deferred if deferred else item.extra
        pending = item.extra.get("_canonical_pending_source_reads")
        read_segments = item.extra.get("canonical_code_navigation_segments", ())
        if (
            navigation.get("canonical_code_navigation_action") in {"read", "search"}
            and navigation.get("canonical_code_navigation_broad")
            or pending
            or any(
                segment.get("canonical_code_navigation_action") in {"read", "search"}
                for segment in read_segments
            )
        ):
            narrowed = {
                "canonical_code_navigation_broad": False,
                "canonical_narrow_source_context": True,
                "canonical_source_line_count": bound,
            }
            extra = dict(item.extra)
            if deferred and deferred.get("_canonical_source_read_output_channels", ["stdout"]) == [
                "stdout"
            ]:
                extra["_canonical_deferred_source_read"] = {**deferred, **narrowed}
            elif navigation.get("canonical_code_navigation_action") in {
                "read",
                "search",
            } and navigation.get("_canonical_source_read_output_channels", ["stdout"]) == [
                "stdout"
            ]:
                extra.update(narrowed)
            if pending:
                extra["_canonical_pending_source_reads"] = [
                    {**record, "scope": {**record["scope"], **narrowed}}
                    if record["scope"].get("_canonical_source_read_output_channels", ["stdout"])
                    == ["stdout"]
                    else record
                    for record in pending
                ]
            if "canonical_code_navigation_segments" in extra:
                extra["canonical_code_navigation_segments"] = [
                    {**segment, **narrowed}
                    if segment.get("canonical_code_navigation_action") in {"read", "search"}
                    and segment.get("_canonical_source_read_output_channels", ["stdout"])
                    == ["stdout"]
                    else segment
                    for segment in extra["canonical_code_navigation_segments"]
                ]
            metadata[prior] = replace(
                item,
                extra=extra,
            )


def _mark_pipeline_read_escape(
    metadata: list[_ShellSegmentMetadata],
    segments: list[_ShellSegment],
    segment_metadata_positions: list[int],
    index: int,
    channels: set[str],
) -> None:
    """A pipeline stage can expose source independently of its stdout."""
    for prior in _pipeline_read_indices(segments, index):
        prior = segment_metadata_positions[prior]
        item = metadata[prior]
        if not item.extra:
            continue
        extra = dict(item.extra)

        def exposed(scope: Mapping[str, Any]) -> dict[str, Any]:
            outputs = set(scope.get("_canonical_source_read_output_channels", ("stdout",)))
            if (
                scope.get("canonical_code_navigation_action") in {"read", "search"}
                and "stdout" in outputs
            ):
                return {
                    **scope,
                    "_canonical_source_read_output_channels": sorted(outputs | channels),
                }
            return dict(scope)

        if extra.get("canonical_code_navigation_action") in {"read", "search"}:
            extra = exposed(extra)
        if "_canonical_deferred_source_read" in extra:
            extra["_canonical_deferred_source_read"] = exposed(
                extra["_canonical_deferred_source_read"]
            )
        if "_canonical_pending_source_reads" in extra:
            extra["_canonical_pending_source_reads"] = [
                {**record, "scope": exposed(record["scope"])}
                for record in extra["_canonical_pending_source_reads"]
            ]
        if "canonical_code_navigation_segments" in extra:
            extra["canonical_code_navigation_segments"] = [
                exposed(scope) for scope in extra["canonical_code_navigation_segments"]
            ]
        metadata[prior] = replace(item, extra=extra)
