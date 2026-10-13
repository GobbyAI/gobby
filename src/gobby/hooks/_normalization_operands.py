"""Per-command operand grammars for shell hook normalization.

Each helper knows one command's option grammar well enough to say which of
its operands name filesystem targets; the segment classifier in
``_normalization_canonical`` decides what those targets mean.
"""

import posixpath
from pathlib import Path

from gobby.hooks._normalization_shell import (
    _SHELL_CONTROL_TOKENS,
    _contains_unexpanded_shell_reference,
    _looks_path_target,
    _rebase_shell_paths,
)

_CURL_SHORT_OPTIONS_WITH_VALUES = frozenset("AbcCdDeEFHKmoPQrTtuwxXYz")
# Options whose value is the next word (or attached) for file-mutation commands.
# Values are timestamps, modes, owners, suffixes, or read-only references, except
# the -t/--target-directory destination, which _file_command_operands returns.
# install -S is GNU's suffix and BSD/macOS's safe-copy flag. Directory mode
# must retain every possible BSD operand, including GNU's possible suffix.
_FILE_COMMAND_VALUE_OPTIONS: dict[str, tuple[str, frozenset[str]]] = {
    "touch": ("Adrt", frozenset({"--date", "--reference", "--time"})),
    "mkdir": ("m", frozenset({"--mode"})),
    "mv": ("St", frozenset({"--suffix", "--target-directory"})),
    "ln": ("St", frozenset({"--suffix", "--target-directory"})),
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
_SORT_VALUE_OPTIONS = frozenset(
    {
        "--output",
        "--key",
        "--field-separator",
        "--buffer-size",
        "--temporary-directory",
        "--files0-from",
        "--compress-program",
        "--random-source",
        "--batch-size",
        "--parallel",
    }
)
_TAR_VALUE_OPTIONS = frozenset(
    {
        "--directory",
        "--cd",
        "--file",
        "--files-from",
        "--exclude",
        "--exclude-from",
        "--strip-components",
        "--group",
        "--exclude-tag",
        "--exclude-tag-all",
        "--exclude-tag-under",
        "--exclude-ignore",
        "--exclude-ignore-recursive",
        "--to-command",
    }
)
_TAR_FLAG_OPTIONS = frozenset(
    {
        "--extract",
        "--get",
        "--absolute-names",
        "--absolute-paths",
        "--insecure",
        "--to-stdout",
        "--diff",
        "--delete",
        "--dereference",
        "--gzip",
        "--gunzip",
        "--exclude-backups",
        "--exclude-caches",
        "--exclude-caches-all",
        "--exclude-caches-under",
        "--exclude-vcs",
        "--exclude-vcs-ignores",
        "--verbose",
        "--keep-old-files",
        "--keep-newer-files",
        "--preserve-permissions",
        "--same-permissions",
        "--no-same-owner",
        "--no-same-permissions",
        "--numeric-owner",
        "--touch",
        "--unlink-first",
        "--bzip2",
        "--xz",
        "--zstd",
        "--lzma",
        "--compress",
        "--uncompress",
    }
)
_TAR_SCOPE_OPTIONS = _TAR_FLAG_OPTIONS | _TAR_VALUE_OPTIONS


def _sort_output_paths(parts: list[str]) -> tuple[bool, list[str]]:
    """Separate sort's output target from input and option operands."""
    paths: list[str] = []
    writes = False
    index = 1
    while index < len(parts):
        word = parts[index]
        if word == "--":
            break
        option = _resolve_long_option(word.split("=", 1)[0], _SORT_VALUE_OPTIONS)
        if option == "--output" and "=" in word:
            writes = True
            paths.append(word.split("=", 1)[1])
        elif word == "-o" or option == "--output":
            writes = True
            index += 1
            if index < len(parts):
                paths.append(parts[index])
        elif word.startswith("-") and not word.startswith("--"):
            for offset, option in enumerate(word[1:], 2):
                if option in "oktST":
                    value = word[offset:]
                    if not value:
                        index += 1
                        value = parts[index] if index < len(parts) else ""
                    if option == "o":
                        writes = True
                        if value:
                            paths.append(value)
                    break
        elif option is not None and "=" not in word:
            index += 1
        index += 1
    return writes, paths


def _tar_extract_paths(parts: list[str], cwd: str | None) -> tuple[bool, list[str], bool]:
    """Track extraction directories without treating archive names as options."""
    extracts = False
    to_stdout = False
    absolute_names = False
    unmodelled = False
    directories: list[str] = []
    directory = cwd
    index = 1
    while index < len(parts):
        word = parts[index]
        if word == "--":
            if extracts and index + 1 < len(parts):
                directories.extend(_rebase_shell_paths(["."], directory))
            break
        option = _resolve_long_option(word.split("=", 1)[0], _TAR_SCOPE_OPTIONS)
        if word.startswith("--") and (option is None or option == "--to-command"):
            unmodelled = True
        if option in {"--extract", "--get"}:
            extracts = True
        elif option in {"--absolute-names", "--absolute-paths", "--insecure"}:
            absolute_names = True
        elif option == "--to-stdout":
            to_stdout = True
        elif option in {"--directory", "--cd"} and "=" in word:
            directory = _rebase_shell_paths([word.split("=", 1)[1]], directory)[0]
            directories.append(directory)
        elif option in _TAR_VALUE_OPTIONS:
            if "=" in word:
                index += 1
                continue
            index += 1
            if option in {"--directory", "--cd"} and index < len(parts):
                directory = _rebase_shell_paths([parts[index]], directory)[0]
                directories.append(directory)
        elif (word.startswith("-") and not word.startswith("--")) or (
            index == 1 and word and not word.startswith("-")
        ):
            old_style = not word.startswith("-")
            options = word[1:] if not old_style else word
            for offset, option in enumerate(options, 1):
                if option == "x":
                    extracts = True
                elif option == "O":
                    to_stdout = True
                elif option == "P":
                    absolute_names = True
                elif option in "fCTXIb":
                    value = "" if old_style else options[offset:]
                    if not value:
                        index += 1
                        value = parts[index] if index < len(parts) else ""
                    if option == "C" and value:
                        directory = _rebase_shell_paths([value], directory)[0]
                        directories.append(directory)
                    if not old_style:
                        break
                elif option not in "xvzjJZpkmUoOPB":
                    unmodelled = True
        elif not word.startswith("-"):
            # A member named before a later -C extracts in the earlier directory.
            directories.extend(_rebase_shell_paths(["."], directory))
        index += 1
    unknown = absolute_names or unmodelled
    return (
        extracts and (unknown or not to_stdout),
        [] if unknown else list(dict.fromkeys(directories or _rebase_shell_paths(["."], cwd))),
        unknown,
    )


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
    if cmd == "install":
        bsd_result = _parse_file_command_operands(
            cmd, parts, short_values.replace("S", ""), long_values
        )
        if bsd_result[2]:
            return bsd_result
    return _parse_file_command_operands(cmd, parts, short_values, long_values)


def _file_transfer_paths(
    cmd: str, parts: list[str], cwd: str | None, base_cwd: Path | None
) -> list[str]:
    """Name copied destinations and, for moves, the removed source paths."""
    operands, target_directory, creates_directories = _file_command_operands(cmd, parts)
    if creates_directories:
        return _rebase_shell_paths(operands, cwd)
    if target_directory is not None:
        sources, destination = operands, target_directory
    elif len(operands) >= 2:
        sources, destination = operands[:-1], operands[-1]
    else:
        return []
    directory_operand = destination in {".", ".."}
    destination = _rebase_shell_paths([destination], cwd)[0]
    probe = Path(destination)
    if not probe.is_absolute() and base_cwd is not None:
        probe = base_cwd / probe
    directory_destination = destination
    if destination in {".", ".."}:
        if not probe.is_absolute():
            return []
        directory_destination = posixpath.normpath(str(probe))
    no_target_directory = "-T" in parts or "--no-target-directory" in parts
    is_directory = not no_target_directory and (
        target_directory is not None
        or directory_operand
        or destination.endswith("/")
        or (probe.is_absolute() and probe.is_dir())
    )
    # BSD cp copies a trailing-slash source's contents directly into the destination.
    # A dynamic basename is likewise covered only by the whole destination directory.
    destinations = (
        [
            directory_destination
            if source.endswith("/")
            or posixpath.basename(source).startswith("~")
            or _contains_unexpanded_shell_reference(posixpath.basename(source))
            or any(char in posixpath.basename(source) for char in "*?[{")
            else posixpath.join(destination, posixpath.basename(source))
            for source in sources
        ]
        if is_directory
        else [destination]
    )
    paths = [*_rebase_shell_paths(sources, cwd), *destinations] if cmd == "mv" else destinations
    return list(
        dict.fromkeys(path.removeprefix("./") for path in paths if _looks_path_target(path))
    )


def _parse_file_command_operands(
    cmd: str, parts: list[str], short_values: str, long_values: frozenset[str]
) -> tuple[list[str], str | None, bool]:
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


_SEARCH_OPTIONS_WITH_VALUES = {
    "-A",
    "-B",
    "-C",
    "-d",
    "-D",
    "-e",
    "-f",
    "-g",
    "-m",
    "--after-context",
    "--before-context",
    "--context",
    "--directories",
    "--devices",
    "--file",
    "--glob",
    "--max-count",
    "--regexp",
}

_GREP_DIRECTORY_OPTIONS = {
    "--directories"[:length] for length in range(4, len("--directories") + 1)
}
_SEARCH_COMMAND_VALUE_OPTIONS = {
    "rg": {
        "-t",
        "-T",
        "-r",
        "-j",
        "-M",
        "-E",
        "--type",
        "--type-not",
        "--replace",
        "--threads",
        "--max-columns",
        "--encoding",
        "--max-depth",
        "--iglob",
        "--ignore-file",
        "--pre",
        "--pre-glob",
        "--dfa-size-limit",
        "--regex-size-limit",
        "--engine",
        "--max-filesize",
        "--type-add",
        "--type-clear",
        "--color",
        "--colors",
        "--context-separator",
        "--field-context-separator",
        "--field-match-separator",
        "--hostname-bin",
        "--hyperlink-format",
        "--path-separator",
        "--sort",
        "--sortr",
        "--generate",
    },
    "git": {"--max-depth", "--threads"},
    "grep": _GREP_DIRECTORY_OPTIONS
    | {
        "--include",
        "--exclude",
        "--exclude-dir",
        "--exclude-from",
        "--include-dir",
        "--label",
        "--binary-files",
        "--group-separator",
    },
}


def _search_value_options(cmd: str) -> set[str]:
    return _SEARCH_OPTIONS_WITH_VALUES | _SEARCH_COMMAND_VALUE_OPTIONS.get(cmd, set())


def _search_has_pattern_option(parts: list[str], start: int, cmd: str) -> bool:
    """Find an e/f option using the same consuming grammar as positional parsing."""
    option_args = _search_value_options(cmd)
    arguments = iter(parts[start:])
    for part in arguments:
        if part == "--":
            break
        if part in {"--regexp", "--file"} or part.startswith(("--regexp=", "--file=")):
            return True
        if part.startswith("--"):
            if part in option_args:
                next(arguments, None)
        elif part.startswith("-"):
            for index, option in enumerate(part[1:], start=1):
                if f"-{option}" in option_args:
                    if option in "ef":
                        return True
                    if index == len(part) - 1:
                        next(arguments, None)
                    break
    return False


def _shell_positional_args_after(
    parts: list[str],
    start: int,
    *,
    cmd: str = "",
    option_args: set[str] | None = None,
) -> list[str]:
    positional: list[str] = []
    skip_next = False
    after_options = False
    if option_args is None:
        option_args = _search_value_options(cmd)
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
            if not part.startswith("--"):
                for index, option in enumerate(part[1:], start=1):
                    if f"-{option}" in option_args:
                        skip_next = index == len(part) - 1
                        break
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
    pattern_from_option = _search_has_pattern_option(before_paths, 2, "git")
    positional = _shell_positional_args_after(before_paths, 2, cmd="git")
    return bool(positional) if pattern_from_option else len(positional) > 1


def _grep_is_recursive(parts: list[str]) -> bool:
    """Recognize recursion flags without treating option operands as flags."""
    recursive = False
    arguments = iter(parts[1:])
    for part in arguments:
        if part == "--":
            break
        if part in {"--recursive", "--dereference-recursive"}:
            recursive = True
            continue
        if part.split("=", 1)[0] in _GREP_DIRECTORY_OPTIONS:
            directories = part.split("=", 1)[1] if "=" in part else next(arguments, None)
            recursive = directories == "recurse"
            continue
        if part.startswith("--"):
            if part in {
                "--regexp",
                "--file",
                "--max-count",
                "--after-context",
                "--before-context",
                "--context",
                "--devices",
                "--binary-files",
                "--label",
                "--include",
                "--exclude",
                "--exclude-from",
                "--exclude-dir",
                "--group-separator",
            }:
                next(arguments, None)
            continue
        if not part.startswith("-"):
            continue
        for index, option in enumerate(part[1:], start=1):
            if option in "rR":
                recursive = True
            if option in "efmABCdD":
                argument = part[index + 1 :] or next(arguments, None)
                if option == "d":
                    recursive = argument == "recurse"
                break
    return recursive


def _search_command_paths(cmd: str, parts: list[str]) -> list[str]:
    def is_path_operand(candidate: str) -> bool:
        return _looks_path_target(candidate) or _contains_unexpanded_shell_reference(candidate)

    if cmd in {"rg", "grep"}:
        positional = _shell_positional_args_after(parts, 1, cmd=cmd)
        pattern_from_option = _search_has_pattern_option(parts, 1, cmd)
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
        positional = _shell_positional_args_after(parts, 2, cmd=cmd)
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
