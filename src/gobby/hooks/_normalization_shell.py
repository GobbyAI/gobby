"""Shell tool identity and command-token helpers."""

from typing import Any

from gobby.hooks._normalization_paths import _append_unique_path

# Tools that run shell commands. ``Bash`` is the canonical runtime name, but
# several adapters and transcripts use shell aliases that should behave the same.
_SHELL_TOOLS = frozenset(
    {
        "Bash",
        "bash",
        "shell",
        "run_command",
        "run_shell_command",
        "RunShellCommand",
        "ShellTool",
        "commandExecution",
        "exec_command",
    }
)

_SHELL_CHAIN_TOKENS = frozenset({"&&", "||", ";", "|"})
_SHELL_INPUT_REDIRECTION_TOKENS = frozenset({"<", "<<", "<<<"})
_SHELL_OUTPUT_REDIRECTION_TOKENS = frozenset({">", ">>", "1>", "1>>"})
_SHELL_CONTROL_TOKENS = (
    _SHELL_CHAIN_TOKENS | _SHELL_INPUT_REDIRECTION_TOKENS | _SHELL_OUTPUT_REDIRECTION_TOKENS
)

# Characters that strongly imply an inline sed/awk script rather than a file path.
_SCRIPT_LIKE_CHARS = frozenset({"{", "}", "$", ";", "(", ")"})


def is_shell_tool(tool_name: Any) -> bool:
    """Return True when ``tool_name`` represents shell command execution."""
    return isinstance(tool_name, str) and tool_name in _SHELL_TOOLS


def canonicalize_shell_tool_name(tool_name: Any) -> Any:
    """Normalize shell aliases to the canonical ``Bash`` tool name."""
    if is_shell_tool(tool_name):
        return "Bash"
    return tool_name


def _get_command_text(tool_input: Any) -> str | None:
    """Extract a shell command string from normalized tool input."""
    if not isinstance(tool_input, dict):
        return None

    command = tool_input.get("command")
    if isinstance(command, str) and command.strip():
        return command

    cmd = tool_input.get("cmd")
    if isinstance(cmd, str) and cmd.strip():
        return cmd

    return None


def _shell_positional_args(parts: list[str]) -> list[str]:
    """Return non-option shell args, excluding obvious control operators."""
    return [
        part
        for part in parts[1:]
        if part and part not in _SHELL_CONTROL_TOKENS and not part.startswith("-")
    ]


def _looks_file_like(candidate: str) -> bool:
    """Return True when ``candidate`` looks like a file path, not an inline script.

    Used to gate sed/awk's last positional arg so we don't classify an inline
    script (``'s/foo/bar/'``, ``'{print $1}'``) as a file that was read.
    """
    if not candidate or any(ch in candidate for ch in _SCRIPT_LIKE_CHARS):
        return False
    # Must carry a path separator or an extension-like dot that isn't a leading/solo dot.
    if "/" in candidate:
        return True
    if "." in candidate and candidate not in {".", ".."}:
        return True
    return False


def _looks_path_target(candidate: str) -> bool:
    """Return True when ``candidate`` is a plausible shell path target."""
    if not candidate or candidate in _SHELL_CONTROL_TOKENS or candidate == "-":
        return False
    if candidate.startswith("-") or candidate.startswith("&"):
        return False
    if any(ch in candidate for ch in _SCRIPT_LIKE_CHARS):
        return False
    return True


def _has_sed_inplace_option(parts: list[str]) -> bool:
    """Return True when a sed command performs in-place editing."""
    for part in parts[1:]:
        if part in {"-i", "--in-place"}:
            return True
        if part.startswith("-i"):
            return True
        if part.startswith("--in-place="):
            return True
    return False


def _has_perl_inplace_option(parts: list[str]) -> bool:
    """Return True when a perl command edits files in place."""
    for part in parts[1:]:
        if part == "-pi" or part.startswith("-pi"):
            return True
        if part == "-i" or part.startswith("-i"):
            return True
    return False


def _extract_redirection_paths(parts: list[str]) -> list[str]:
    """Extract explicit output redirection targets from shell tokens."""
    paths: list[str] = []
    for idx, token in enumerate(parts[:-1]):
        if token not in _SHELL_OUTPUT_REDIRECTION_TOKENS:
            continue
        candidate = parts[idx + 1]
        if _looks_path_target(candidate):
            _append_unique_path(paths, candidate)
    return paths
