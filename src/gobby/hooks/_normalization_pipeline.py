"""Safety checks for shell stdin consumers, pipeline stages and output markers."""

from pathlib import Path

from gobby.hooks._normalization_base_commands import classify_base_command
from gobby.hooks._normalization_redirects import has_mutating_output_redirection
from gobby.hooks._normalization_shell import (
    ShellToken,
    has_shell_input_redirection,
    shell_token_values,
    strip_output_redirections,
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
    if cmd not in _GCODE_PIPELINE_READ_ONLY_FILTERS:
        return _is_read_only_python_pipeline(parts)
    command_tokens = tokens[-len(parts) :]
    plain_tokens = strip_output_redirections(command_tokens)
    if raw_parts is not None:
        plain_ids = {id(token) for token in plain_tokens}
        raw_parts = tuple(
            raw
            for token, raw in zip(command_tokens, raw_parts, strict=True)
            if id(token) in plain_ids
        )
    # Only stdin-only filters can skip ordinary command classification. Explicit
    # file operands retain the same read or mutation semantics at every stage.
    metadata = classify_base_command(
        shell_token_values(plain_tokens),
        cwd,
        shell_stdin=True,
        base_cwd=base_cwd,
        raw_parts=raw_parts,
    )
    return (
        not metadata.paths
        and not metadata.repo_mutation
        and metadata.proven_read_only
        and (cmd != "sed" or metadata.kind == "read")
    )


def _is_neutral_echo_segment(tokens: list[ShellToken], parts: list[str]) -> bool:
    """Return True for side-effect-free ``echo`` segments used as output markers."""
    if not parts or shell_command_name(parts[0]) != "echo":
        return False
    if has_shell_input_redirection(tokens) or has_mutating_output_redirection(tokens):
        return False
    return not any(ch in part for part in parts[1:] for ch in _ECHO_UNSAFE_CHARS)
