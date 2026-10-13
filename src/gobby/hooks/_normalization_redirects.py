"""Shell redirection destinations, mutation policy and path extraction."""

import re

from gobby.hooks._normalization_paths import _append_unique_path
from gobby.hooks._normalization_shell import (
    _COMMAND_SUBSTITUTION,
    _FD_OUTPUT_REDIRECTION_RE,
    _SHELL_OUTPUT_REDIRECTION_TOKENS,
    ShellToken,
    _contains_unexpanded_shell_reference,
    _looks_path_target,
    is_fd_duplication_token,
    is_shell_output_redirection_token,
    is_unquoted_shell_control_token,
)

# Output sinks that never mutate files; redirecting to them is not a write.
_BENIGN_REDIRECT_TARGETS = frozenset(
    {
        "/dev/null",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/tty",
        "/dev/fd/1",
        "/dev/fd/2",
        "/proc/self/fd/1",
        "/proc/self/fd/2",
    }
)


def is_benign_redirect_target(path: str) -> bool:
    """Return True when redirecting output to ``path`` cannot mutate a file."""
    return path in _BENIGN_REDIRECT_TARGETS


def has_mutating_output_redirection(tokens: list[ShellToken]) -> bool:
    """Return True when any output redirection may write somewhere other than a benign sink.

    Fails closed: a redirection with a missing or undeterminable target counts
    as mutating.
    """
    for idx, token in enumerate(tokens):
        if not is_shell_output_redirection_token(token):
            continue
        if idx + 1 >= len(tokens):
            return True
        candidate = tokens[idx + 1]
        if is_unquoted_shell_control_token(candidate):
            return True
        if not is_benign_redirect_target(candidate.value):
            return True
    return False


def redirects_stdout_to_file(tokens: list[ShellToken]) -> bool:
    """Prove stdout's final destination is a literal, non-benign file."""
    file_redirected = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if is_fd_duplication_token(token):
            fd = re.match(r"\d+", token.value)
            target_fd = (
                fd.group().lstrip("0") if fd else ("1" if token.value.startswith(">") else "0")
            )
            if target_fd == "1":
                file_redirected = False
            continue
        if token.quoted:
            continue
        target = tokens[index] if index < len(tokens) else None
        fd_redirect = re.fullmatch(r"(\d+)(?:<|>|>>)", token.value)
        if fd_redirect and fd_redirect.group(1).lstrip("0") == "1" and target is not None:
            if not target.quoted and target.value == "&":
                file_redirected = False
                continue
        if token.value not in {">", ">>", "1>", "1>>", "&>", "&>>", ">&", "1>&", ">|"}:
            continue
        if target is None:
            file_redirected = False
            continue
        if token.value in {">&", "1>&"}:
            if re.fullmatch(r"\d+|-", target.value):
                file_redirected = False
                index += 1
                continue
        file_redirected = (
            not is_unquoted_shell_control_token(target)
            and not is_benign_redirect_target(target.value)
            and not target.value.startswith(("/dev/fd/", "/proc/self/fd/"))
            and not _contains_unexpanded_shell_reference(target.value)
            and not _COMMAND_SUBSTITUTION.search(target.value)
            and (target.quoted or not any(char in target.value for char in "~*?[{"))
        )
    return file_redirected


def extract_redirection_paths(tokens: list[ShellToken]) -> list[str]:
    """Extract explicit output redirection targets from quote-aware shell tokens."""
    paths: list[str] = []
    for idx, token in enumerate(tokens[:-1]):
        if not is_shell_output_redirection_token(token):
            continue
        candidate = tokens[idx + 1]
        if is_unquoted_shell_control_token(candidate):
            continue
        if is_benign_redirect_target(candidate.value):
            continue
        if _looks_path_target(candidate.value):
            _append_unique_path(paths, candidate.value)
    return paths


def _extract_redirection_paths(parts: list[str]) -> list[str]:
    """Extract explicit output redirection targets from shell tokens."""
    paths: list[str] = []
    for idx, token in enumerate(parts[:-1]):
        if token not in _SHELL_OUTPUT_REDIRECTION_TOKENS and not _FD_OUTPUT_REDIRECTION_RE.match(
            token
        ):
            continue
        candidate = parts[idx + 1]
        if is_benign_redirect_target(candidate):
            continue
        if _looks_path_target(candidate):
            _append_unique_path(paths, candidate)
    return paths
