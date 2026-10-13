"""Shell redirection destinations, mutation policy and path extraction."""

import re
from collections.abc import Mapping
from typing import Any

from gobby.hooks._normalization_paths import _append_unique_path
from gobby.hooks._normalization_shell import (
    _FD_OUTPUT_REDIRECTION_RE,
    _SHELL_OUTPUT_REDIRECTION_TOKENS,
    ShellToken,
    _contains_unexpanded_shell_reference,
    _looks_path_target,
    _strip_shell_wrappers,
    is_shell_output_redirection_token,
    is_unquoted_shell_control_token,
    scan_shell_command,
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


def source_output_destinations(tokens: list[ShellToken]) -> dict[str, str]:
    """Resolve inherited stdout/stderr destinations in shell redirection order."""
    descriptors = {"1": "stdout", "2": "stderr"}
    for index, token in enumerate(tokens):
        if token.quoted:
            continue
        duplicate = re.fullmatch(r"(\d*)[<>]&(\d+|-)", token.value)
        if duplicate:
            fd, target = duplicate.groups()
            fd = (fd.lstrip("0") or "0") if fd else ("0" if token.value.startswith("<") else "1")
            descriptors[fd] = (
                descriptors.get(target.lstrip("0") or "0", "unknown") if target != "-" else "closed"
            )
            continue
        redirect = re.fullmatch(r"(\d*)(>|>>|>\||>&|<|<&)", token.value)
        both = token.value in {"&>", "&>>", ">&"}
        if not redirect and not both:
            continue
        target = tokens[index + 1].value if index + 1 < len(tokens) else ""
        fd = (redirect[1].lstrip("0") or "0") if redirect and redirect[1] else "1"
        if redirect and redirect[2].startswith("<") and not redirect[1]:
            fd = "0"
        split_duplicate = target == "&" and index + 2 < len(tokens)
        if split_duplicate:
            target = tokens[index + 2].value
        if (
            redirect
            and (redirect[2] in {">&", "<&"} or split_duplicate)
            and re.fullmatch(r"\d+|-", target)
        ):
            descriptors[fd] = (
                descriptors.get(target.lstrip("0") or "0", "unknown") if target != "-" else "closed"
            )
            continue
        if redirect and redirect[2].startswith("<"):
            continue
        if target in {"/dev/stdout", "/dev/fd/1", "/proc/self/fd/1"}:
            destination = descriptors.get("1", "unknown")
        elif target in {"/dev/stderr", "/dev/fd/2", "/proc/self/fd/2"}:
            destination = descriptors.get("2", "unknown")
        elif target == "/dev/tty":
            destination = "tty"
        elif target == "/dev/null":
            destination = "null"
        elif device := re.fullmatch(r"(?:/dev/fd/|/proc/self/fd/)(\d+)", target):
            destination = descriptors.get(device[1].lstrip("0") or "0", "unknown")
        elif (
            not target
            or _contains_unexpanded_shell_reference(target)
            or (
                index + 1 < len(tokens)
                and not tokens[index + 1].quoted
                and any(char in target for char in "~*?[{")
            )
        ):
            destination = "unknown"
        else:
            destination = "file"
        descriptors[fd] = destination
        if both:
            descriptors["2"] = destination
    return {**descriptors, "stdout": descriptors["1"], "stderr": descriptors["2"]}


def tee_output_channels(paths: list[str], tokens: list[ShellToken]) -> set[str]:
    """Recognize terminal and stderr copies, including equivalent descriptor names."""
    destinations = source_output_destinations(tokens)
    channels: set[str] = set()
    for path in paths:
        if path == "/dev/tty":
            channels.add("tty")
        elif path == "/dev/stderr":
            channels.add(destinations["stderr"])
        elif path == "/dev/stdout":
            channels.add(destinations["stdout"])
        elif match := re.fullmatch(r"(?:/dev/fd/|/proc/self/fd/)(\d+)", path):
            channels.add(destinations.get(match[1].lstrip("0") or "0", "unknown"))
    for index, token in enumerate(tokens[:-1]):
        following = tokens[index + 1]
        if (
            not token.quoted
            and token.value == ">"
            and not following.quoted
            and following.value.startswith("(")
        ):
            try:
                body = scan_shell_command(following.value[1:-1]).tokens
                words = _strip_shell_wrappers([part.value for part in body])
                # A single cat forwards stdin to its resolved stdout. Compound
                # consumers and other programs may expose it by another route.
                opaque = (
                    not words
                    or words[0].rsplit("/", 1)[-1] != "cat"
                    or any(
                        not part.quoted and part.value in {";", "&&", "||", "|", "&", "\n"}
                        for part in body
                    )
                )
                channel = "unknown" if opaque else source_output_destinations(body)["stdout"]
                channels.add(destinations.get(channel, channel))
            except ValueError:
                channels.add("unknown")
    return channels - {"stdout", "file", "closed", "null"}


def redirect_source_read_metadata(
    extra: Mapping[str, Any] | None, tokens: list[ShellToken]
) -> dict[str, Any] | None:
    """Keep each source read only when its output can reach the caller."""
    if not extra:
        return None
    result = dict(extra)
    destinations = source_output_destinations(tokens)

    def redirected(scope: Mapping[str, Any]) -> dict[str, Any] | None:
        if scope.get("canonical_code_navigation_action") != "read":
            return dict(scope)
        channels = {
            destinations.get(channel, channel)
            for channel in scope.get("_canonical_source_read_output_channels", ("stdout",))
        } - {"file", "closed", "null"}
        if not channels:
            return None
        return {**scope, "_canonical_source_read_output_channels": sorted(channels)}

    if "canonical_code_navigation_segments" in result:
        result["canonical_code_navigation_segments"] = [
            mapped
            for segment in result["canonical_code_navigation_segments"]
            if (mapped := redirected(segment)) is not None
        ]
    deferred_key = "_canonical_deferred_source_read"
    if deferred_key in result:
        mapped = redirected(result[deferred_key])
        if mapped is None:
            result.pop(deferred_key)
        else:
            result[deferred_key] = mapped
    if "_canonical_pending_source_reads" in result:
        result["_canonical_pending_source_reads"] = [
            {**record, "scope": mapped}
            for record in result["_canonical_pending_source_reads"]
            if (mapped := redirected(record["scope"])) is not None
        ]
    if result.get("canonical_code_navigation_action") == "read":
        child_scopes = [
            *result.get("canonical_code_navigation_segments", ()),
            *(record["scope"] for record in result.get("_canonical_pending_source_reads", ())),
        ]
        if deferred := result.get("_canonical_deferred_source_read"):
            child_scopes.append(deferred)
        read_scopes = [
            scope
            for scope in child_scopes
            if scope.get("canonical_code_navigation_action") == "read"
        ]
        has_children = any(
            key in extra
            for key in (
                "canonical_code_navigation_segments",
                "_canonical_pending_source_reads",
                "_canonical_deferred_source_read",
            )
        )
        mapped = redirected(result) if not has_children else None
        if has_children and read_scopes:
            mapped = {
                **result,
                "_canonical_source_read_output_channels": sorted(
                    {
                        channel
                        for scope in read_scopes
                        for channel in scope.get(
                            "_canonical_source_read_output_channels", ("stdout",)
                        )
                    }
                ),
            }
        if mapped is None:
            for key in tuple(result):
                if key != "canonical_code_navigation_segments" and key.startswith(
                    ("canonical_code_navigation_", "canonical_source_", "canonical_narrow_")
                ):
                    result.pop(key)
            # A discarded read must not be reconstructed by the read-kind fallback.
            result["canonical_code_navigation_broad"] = False
        else:
            result.update(mapped)
    return result


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
        if re.fullmatch(r"\d*>&", token.value) and re.fullmatch(r"\d+|-", candidate.value):
            continue
        if is_unquoted_shell_control_token(candidate):
            return True
        if not is_benign_redirect_target(candidate.value):
            return True
    return False


def extract_redirection_paths(tokens: list[ShellToken]) -> list[str]:
    """Extract explicit output redirection targets from quote-aware shell tokens."""
    paths: list[str] = []
    for idx, token in enumerate(tokens[:-1]):
        if not is_shell_output_redirection_token(token):
            continue
        candidate = tokens[idx + 1]
        if re.fullmatch(r"\d*>&", token.value) and re.fullmatch(r"\d+|-", candidate.value):
            continue
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
