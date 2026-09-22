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
    cwd: str | None = None
    loop_binding_variable: str | None = None
    shell_words: tuple[str, ...] = ()
    shell_raw_words: tuple[str, ...] = ()


def _split_shell_segments(tokens: list[ShellToken]) -> list[_ShellSegment]:
    """Split tokenized shell input at unquoted chain operators."""
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
        if (
            item.extra
            and item.extra.get("canonical_code_navigation_action") == "read"
            and item.extra.get("canonical_code_navigation_broad")
        ):
            metadata[prior] = replace(
                item,
                extra={
                    **item.extra,
                    "canonical_code_navigation_broad": False,
                    "canonical_narrow_source_context": True,
                    "canonical_source_line_count": bound,
                },
            )
