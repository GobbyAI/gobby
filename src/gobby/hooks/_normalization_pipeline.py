"""Safety checks for shell stdin consumers, pipeline stages and output markers."""

from pathlib import Path

from gobby.hooks._normalization_sed import classify_sed_segment
from gobby.hooks._normalization_shell import (
    ShellToken,
    has_mutating_output_redirection,
    has_shell_input_redirection,
)
from gobby.hooks._python_pipeline_classifier import (
    _inline_interpreter_parts,
    _is_read_only_python_pipeline,
)
from gobby.hooks.code_navigation import shell_command_name

_GCODE_PIPELINE_READ_ONLY_FILTERS = frozenset(
    {"cat", "cut", "grep", "head", "jq", "rg", "sed", "sort", "tail", "tr", "uniq", "wc"}
)
# Characters in echo arguments that imply command substitution rather than a plain marker.
_ECHO_UNSAFE_CHARS = frozenset({"$", "`"})


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


def _is_read_only_pipeline_stage(
    tokens: list[ShellToken],
    parts: list[str],
    *,
    raw_parts: tuple[str, ...] | None = None,
    cwd: str | None = None,
    base_cwd: Path | None = None,
) -> bool:
    if not parts:
        return False
    if has_shell_input_redirection(tokens) or has_mutating_output_redirection(tokens):
        return False
    cmd = shell_command_name(parts[0])
    if cmd == "sed":
        return classify_sed_segment(parts, cwd, raw_parts, base_cwd).kind == "read"
    return cmd in _GCODE_PIPELINE_READ_ONLY_FILTERS or _is_read_only_python_pipeline(parts)


def _is_neutral_echo_segment(tokens: list[ShellToken], parts: list[str]) -> bool:
    """Return True for side-effect-free ``echo`` segments used as output markers."""
    if not parts or shell_command_name(parts[0]) != "echo":
        return False
    if has_shell_input_redirection(tokens) or has_mutating_output_redirection(tokens):
        return False
    return not any(ch in part for part in parts[1:] for ch in _ECHO_UNSAFE_CHARS)
