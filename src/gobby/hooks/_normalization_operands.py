"""Per-command operand grammars for shell hook normalization.

Each helper knows one command's option grammar well enough to say which of
its operands name filesystem targets; the segment classifier in
``_normalization_canonical`` decides what those targets mean.
"""

import posixpath

from gobby.hooks._normalization_shell import (
    _SHELL_CONTROL_TOKENS,
    _contains_unexpanded_shell_reference,
    _looks_path_target,
)

_CURL_SHORT_OPTIONS_WITH_VALUES = frozenset("AbcCdDeEFHKmoPQrTtuwxXYz")
# Options whose value is the next word (or attached) for file-mutation commands.
# Values are timestamps, modes, owners, suffixes, or read-only references, except
# the -t/--target-directory destination, which _file_command_operands returns.
# install -S is GNU's suffix; on BSD/macOS it is a flag, so its next word is
# dropped there, which still leaves the last operand as the destination.
_FILE_COMMAND_VALUE_OPTIONS: dict[str, tuple[str, frozenset[str]]] = {
    "touch": ("Adrt", frozenset({"--date", "--reference", "--time"})),
    "mkdir": ("m", frozenset({"--mode"})),
    "mv": ("St", frozenset({"--suffix", "--target-directory"})),
    "cp": ("St", frozenset({"--no-preserve", "--sparse", "--suffix", "--target-directory"})),
    "install": (
        "gmoSt",
        frozenset(
            {"--group", "--mode", "--owner", "--strip-program", "--suffix", "--target-directory"}
        ),
    ),
}
# Exact flags that GNU getopt matches before treating them as abbreviations
# of a longer value-taking option (install --strip vs --strip-program).
_LONG_FLAGS_SHADOWING_VALUE_OPTIONS = frozenset({"--strip"})
_INSTALL_LONG_DIRECTORY_CANDIDATES = frozenset({"--debug", "--directory"})


def _resolve_long_option(option: str, names: frozenset[str]) -> str | None:
    """Resolve ``option`` exactly or as GNU's unambiguous abbreviation of one of ``names``."""
    if option in names:
        return option
    if option in _LONG_FLAGS_SHADOWING_VALUE_OPTIONS:
        return None
    matches = [name for name in names if name.startswith(option)]
    return matches[0] if len(matches) == 1 else None


def _file_command_operands(cmd: str, parts: list[str]) -> tuple[list[str], str | None, bool]:
    """Return operands, the ``-t``/``--target-directory`` value, and install directory mode.

    Option values never become operands, including clustered short options
    (``touch -mt STAMP``), abbreviated long options, and long options given a
    separate value. ``install -d`` creates every operand.
    """
    short_values, long_values = _FILE_COMMAND_VALUE_OPTIONS.get(cmd, ("", frozenset()))
    operands: list[str] = []
    target_directory: str | None = None
    creates_directories = False
    after_options = False
    index = 1
    while index < len(parts):
        part = parts[index]
        index += 1
        if not part or part in _SHELL_CONTROL_TOKENS:
            continue
        if after_options or part == "-" or not part.startswith("-"):
            operands.append(part)
            continue
        if part == "--":
            after_options = True
            continue
        option, value = part, None
        if part.startswith("--"):
            name, has_value, attached = part.partition("=")
            resolved = _resolve_long_option(name, long_values)
            if resolved is None:
                if cmd == "install" and (
                    _resolve_long_option(name, _INSTALL_LONG_DIRECTORY_CANDIDATES) == "--directory"
                ):
                    creates_directories = True
                continue
            option, value = resolved, attached if has_value else None
        else:
            for offset, flag in enumerate(part[1:], start=2):
                if flag in short_values:
                    option, value = f"-{flag}", part[offset:] or None
                    break
                if cmd == "install" and flag == "d":
                    creates_directories = True
            else:
                continue
        if value is None:
            value = parts[index] if index < len(parts) else ""
            index += 1
        if option in {"-t", "--target-directory"} and cmd != "touch":
            target_directory = value
    return operands, target_directory, creates_directories


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


def _git_restore_positional_args_after(parts: list[str], start: int) -> list[str]:
    return [
        candidate
        for candidate in _shell_positional_args_after(
            parts,
            start,
            option_args={"-s", "--source", "--pathspec-from-file"},
        )
        if _looks_path_target(candidate)
    ]


_FIND_MUTATION_PREDICATES = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir"})


def _find_has_mutation_predicate(parts: list[str]) -> bool:
    """Return whether a find invocation may mutate matched paths."""
    index = 1
    while index < len(parts):
        part = parts[index]
        if part not in _FIND_MUTATION_PREDICATES:
            index += 1
            continue
        if part != "-exec" or parts[index + 1 : index + 2] != ["stat"]:
            return True
        end = next(
            (
                offset
                for offset in range(index + 2, len(parts))
                if parts[offset] == "+" and parts[offset - 1] == "{}"
            ),
            None,
        )
        if end is None or any(
            _contains_unexpanded_shell_reference(arg) for arg in parts[index + 2 : end]
        ):
            return True
        index = end + 1
    return False


def _git_grep_is_revision_scoped(parts: list[str]) -> bool:
    """Return whether git grep names a revision before its path separator."""
    before_paths = parts[: parts.index("--")] if "--" in parts else parts
    pattern_from_option = any(
        part in {"-e", "--regexp", "-f", "--file"}
        or (part.startswith("-e") and not part.startswith("--") and part != "-e")
        or (part.startswith("-f") and not part.startswith("--") and part != "-f")
        or part.startswith("--regexp=")
        or part.startswith("--file=")
        for part in before_paths[2:]
    )
    positional = _shell_positional_args_after(before_paths, 2)
    return bool(positional) if pattern_from_option else len(positional) > 1


def _search_command_paths(cmd: str, parts: list[str]) -> list[str]:
    def is_path_operand(candidate: str) -> bool:
        return _looks_path_target(candidate) or _contains_unexpanded_shell_reference(candidate)

    if cmd in {"rg", "grep"}:
        positional = _shell_positional_args_after(parts, 1)
        pattern_from_option = any(
            part in {"-e", "--regexp"} or part.startswith("-e") or part.startswith("--regexp=")
            for part in parts[1:]
        )
        lists_files = cmd == "rg" and "--files" in parts[1:]
        candidate_paths = positional if pattern_from_option or lists_files else positional[1:]
        return [path for path in candidate_paths if is_path_operand(path)]

    if cmd == "git":
        if len(parts) <= 1 or parts[1] != "grep":
            return []
        if "--" in parts:
            separator_index = parts.index("--")
            return [path for path in parts[separator_index + 1 :] if is_path_operand(path)]
        if _git_grep_is_revision_scoped(parts):
            return []
        positional = _shell_positional_args_after(parts, 2)
        return [path for path in positional[1:] if is_path_operand(path)]

    if cmd == "find":
        paths: list[str] = []
        for part in parts[1:]:
            if part == "--":
                continue
            if part in _SHELL_CONTROL_TOKENS or not part:
                continue
            if part.startswith("-") or part in {"!", "(", ")"}:
                break
            if is_path_operand(part):
                paths.append(part)
        return paths

    return []
