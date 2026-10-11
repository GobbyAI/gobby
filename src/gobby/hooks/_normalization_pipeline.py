"""Safety checks for read-only shell pipeline stages and output markers."""

from gobby.hooks._normalization_shell import (
    ShellToken,
    _has_sed_inplace_option,
    has_mutating_output_redirection,
    has_shell_input_redirection,
)
from gobby.hooks._python_pipeline_classifier import _is_read_only_python_pipeline
from gobby.hooks.code_navigation import shell_command_name

_GCODE_PIPELINE_READ_ONLY_FILTERS = frozenset(
    {"cat", "cut", "grep", "head", "jq", "rg", "sed", "sort", "tail", "tr", "uniq", "wc"}
)
# Characters in echo arguments that imply command substitution rather than a plain marker.
_ECHO_UNSAFE_CHARS = frozenset({"$", "`"})


def _is_read_only_pipeline_stage(tokens: list[ShellToken], parts: list[str]) -> bool:
    if not parts:
        return False
    if has_shell_input_redirection(tokens) or has_mutating_output_redirection(tokens):
        return False
    cmd = shell_command_name(parts[0])
    if cmd == "sed" and _has_sed_inplace_option(parts):
        return False
    return cmd in _GCODE_PIPELINE_READ_ONLY_FILTERS or _is_read_only_python_pipeline(parts)


def _is_neutral_echo_segment(tokens: list[ShellToken], parts: list[str]) -> bool:
    """Return True for side-effect-free ``echo`` segments used as output markers."""
    if not parts or shell_command_name(parts[0]) != "echo":
        return False
    if has_shell_input_redirection(tokens) or has_mutating_output_redirection(tokens):
        return False
    return not any(ch in part for part in parts[1:] for ch in _ECHO_UNSAFE_CHARS)
