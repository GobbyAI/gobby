"""Per-command operand grammars for shell hook normalization.

Each helper knows one command's option grammar well enough to say which of
its operands name filesystem targets; the segment classifier in
``_normalization_canonical`` decides what those targets mean.
"""

import posixpath

from gobby.hooks._normalization_shell import _SHELL_CONTROL_TOKENS, _looks_path_target

_CURL_SHORT_OPTIONS_WITH_VALUES = frozenset("AbcCdDeEFHKmoPQrTtuwxXYz")


def _truncate_positional_paths(parts: list[str]) -> list[str]:
    """Return path operands from a simple ``truncate`` command."""
    positional: list[tuple[str, bool]] = []
    skip_next = False
    for index, part in enumerate(parts[1:], start=1):
        if skip_next:
            skip_next = False
            continue
        if part == "--":
            positional.extend((candidate, True) for candidate in parts[index + 1 :])
            break
        if part in _SHELL_CONTROL_TOKENS or not part:
            continue
        if part in {"-s", "--size", "-r", "--reference"}:
            skip_next = True
            continue
        if part.startswith(("--size=", "--reference=")) or part.startswith("-"):
            continue
        positional.append((part, False))
    return [
        candidate
        for candidate, after_options in positional
        if _looks_path_target(candidate)
        or (
            after_options
            and candidate
            and candidate not in _SHELL_CONTROL_TOKENS
            and candidate != "-"
        )
    ]


def _curl_output_paths(parts: list[str]) -> tuple[bool, list[str]]:
    output_dir: str | None = None
    for index, part in enumerate(parts[1:], start=1):
        if part == "--output-dir" and index + 1 < len(parts):
            output_dir = parts[index + 1]
        elif part.startswith("--output-dir="):
            output_dir = part.partition("=")[2]

    paths: list[str] = []
    writes_file = False
    unknown_output = False
    index = 1
    while index < len(parts):
        part = parts[index]
        output: str | None = None
        short_config = False
        short_remote_name = False
        if part in {"-o", "--output"}:
            if index + 1 >= len(parts):
                return True, []
            output = parts[index + 1]
            index += 1
        elif part.startswith("--output="):
            output = part.partition("=")[2]
        elif part.startswith("-") and not part.startswith("--"):
            for option_index, option in enumerate(part[1:], start=1):
                if option == "o":
                    output = part[option_index + 1 :]
                    if not output:
                        if index + 1 >= len(parts):
                            return True, []
                        output = parts[index + 1]
                        index += 1
                    break
                if option == "O":
                    short_remote_name = True
                elif option == "K":
                    short_config = True
                    break
                elif option in _CURL_SHORT_OPTIONS_WITH_VALUES:
                    break

        if output is not None and output != "-":
            writes_file = True
            if output_dir and not posixpath.isabs(output):
                output = posixpath.join(output_dir, output)
            if output not in paths:
                paths.append(output)

        if short_config or part in {"-K", "--config"} or part.startswith("--config="):
            unknown_output = True

        remote_name = short_remote_name or part in {"-O", "--remote-name", "--remote-name-all"}
        if remote_name:
            writes_file = True
            if output_dir:
                if output_dir not in paths:
                    paths.append(output_dir)
            else:
                unknown_output = True
        index += 1

    return writes_file or unknown_output, [] if unknown_output else paths


def _shell_positional_args_after(
    parts: list[str],
    start: int,
    *,
    option_args: set[str] | None = None,
) -> list[str]:
    positional: list[str] = []
    skip_next = False
    after_options = False
    if option_args is None:
        option_args = {
            "-A",
            "-B",
            "-C",
            "-e",
            "-f",
            "-g",
            "-m",
            "--after-context",
            "--before-context",
            "--context",
            "--file",
            "--glob",
            "--max-count",
            "--regexp",
        }
    for part in parts[start:]:
        if skip_next:
            skip_next = False
            continue
        if part in _SHELL_CONTROL_TOKENS or not part:
            continue
        if not after_options and part == "--":
            after_options = True
            continue
        if not after_options and part in option_args:
            skip_next = True
            continue
        if not after_options and part.startswith("--") and "=" in part:
            continue
        if not after_options and part.startswith("-") and part != "-":
            continue
        positional.append(part)
    return positional


def _git_add_positional_args_after(parts: list[str], start: int) -> list[str]:
    return _shell_positional_args_after(
        parts,
        start,
        option_args={"--chmod", "--pathspec-from-file"},
    )


def _search_command_paths(cmd: str, parts: list[str]) -> list[str]:
    if cmd in {"rg", "grep"}:
        positional = _shell_positional_args_after(parts, 1)
        pattern_from_option = any(
            part in {"-e", "--regexp"} or part.startswith("-e") or part.startswith("--regexp=")
            for part in parts[1:]
        )
        candidate_paths = positional if pattern_from_option else positional[1:]
        return [path for path in candidate_paths if _looks_path_target(path)]

    if cmd == "git":
        if len(parts) <= 1 or parts[1] != "grep":
            return []
        if "--" in parts:
            separator_index = parts.index("--")
            return [path for path in parts[separator_index + 1 :] if _looks_path_target(path)]
        positional = _shell_positional_args_after(parts, 2)
        return [path for path in positional[1:] if _looks_path_target(path)]

    if cmd == "find":
        paths: list[str] = []
        for part in parts[1:]:
            if part == "--":
                continue
            if part in _SHELL_CONTROL_TOKENS or not part:
                continue
            if part.startswith("-") or part in {"!", "(", ")"}:
                break
            if _looks_path_target(part):
                paths.append(part)
        return paths

    return []
