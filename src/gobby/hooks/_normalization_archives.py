"""Archive destinations are known; the paths stored inside an archive are not."""

from gobby.hooks._normalization_operands import _tar_extract_paths
from gobby.hooks._normalization_segments import _ShellSegmentMetadata
from gobby.hooks._normalization_shell import _rebase_shell_paths


def _archive_options(
    parts: list[str],
    *,
    short_values: str,
    long_values: frozenset[str] = frozenset(),
    directory_options: frozenset[str] = frozenset(),
    old_style: bool = False,
) -> tuple[set[str], list[str]]:
    """Keep option operands out of mode flags and destination switches."""
    flags: set[str] = set()
    directories: list[str] = []
    index = 1
    while index < len(parts):
        word = parts[index]
        if word == "--":
            break
        if word.startswith("--"):
            option, separator, value = word.partition("=")
            flags.add(option)
            if option in long_values:
                if not separator:
                    index += 1
                    value = parts[index] if index < len(parts) else "$UNRESOLVED"
                if option in directory_options:
                    directories.append(value)
        elif word.startswith("-") or (old_style and index == 1):
            old_options = old_style and index == 1 and not word.startswith("-")
            options = word if old_options else word[1:]
            for offset, option in enumerate(options, 1):
                flags.add(option)
                if option not in short_values:
                    continue
                value = "" if old_options else options[offset:]
                if not value:
                    index += 1
                    value = parts[index] if index < len(parts) else "$UNRESOLVED"
                if option in directory_options:
                    directories.append(value)
                if not old_options:
                    break
        index += 1
    return flags, directories


def _unzip_destination(parts: list[str], cwd: str | None) -> list[str] | None:
    flags, directories = _archive_options(
        parts, short_values="dP", directory_options=frozenset({"d"})
    )
    if flags.intersection("Zlptvczh"):
        return None
    return _rebase_shell_paths(directories or ["."], cwd)


def _option_directories(parts: list[str], cwd: str | None, short: str, long: str) -> list[str]:
    directories: list[str] = []
    index = 1
    while index < len(parts):
        word = parts[index]
        if word == "--":
            break
        option = word.casefold()
        if option in {short, long}:
            index += 1
            directories.append(parts[index] if index < len(parts) else "$UNRESOLVED")
        elif option.startswith(long + "="):
            directories.append(word.partition("=")[2])
        elif option.startswith(short) and len(word) > len(short):
            directories.append(word[len(short) :])
        index += 1
    return _rebase_shell_paths(directories, cwd)


def _other_archive_destinations(cmd: str, parts: list[str], cwd: str | None) -> list[str] | None:
    if cmd == "unzip":
        return _unzip_destination(parts, cwd)
    if cmd in {"7z", "7za", "7zz", "7zr", "unrar", "rar"}:
        operation = next((i for i, word in enumerate(parts[1:], 1) if not word.startswith("-")), 0)
        if not operation or parts[operation].casefold() not in {"x", "e"}:
            return None
        if cmd in {"unrar", "rar"}:
            output_directories = _option_directories(parts, cwd, "-op", "--output-directory")
            operands = [word for word in parts[operation + 1 :] if not word.startswith("-")]
            if output_directories:
                # Do not guess how a second positional target combines with -op.
                return output_directories if len(operands) == 1 else ["$UNRESOLVED"]
            # An optional trailing destination is otherwise ambiguous with a member selector.
            paths = [operands[-1]] if len(operands) > 1 and operands[-1].endswith("/") else ["."]
            if len(operands) > 1 and not operands[-1].endswith("/"):
                paths.append(operands[-1])
            return _rebase_shell_paths(paths, cwd)
        options = parts[1 : parts.index("--")] if "--" in parts else parts[1:]
        if "-so" in options:
            return None
        return _option_directories(parts, cwd, "-o", "--output-directory") or _rebase_shell_paths(
            ["."], cwd
        )
    if cmd == "ditto" and "-x" in parts[1:]:
        return _rebase_shell_paths(parts[-1:] if len(parts) > 3 else ["$UNRESOLVED"], cwd)
    if cmd in {"cpio", "pax", "jar"}:
        flags, directories = _archive_options(
            parts,
            short_values={"cpio": "EFHMRIOCD", "pax": "bfopsxUGT", "jar": "fCm"}[cmd],
            long_values=frozenset(
                {
                    "--file",
                    "--format",
                    "--message",
                    "--owner",
                    "--pattern-file",
                    "--block-size",
                    "--io-size",
                    "--rsh-command",
                    "--directory",
                    "--dir",
                    "--manifest",
                    "--main-class",
                    "--module-version",
                }
            ),
            directory_options=frozenset({"D", "--directory"})
            if cmd == "cpio"
            else frozenset({"C", "--dir"})
            if cmd == "jar"
            else frozenset(),
            old_style=cmd == "jar",
        )
        if cmd == "cpio":
            if flags.isdisjoint({"i", "--extract"}) or not flags.isdisjoint({"t", "--list"}):
                return None
            if "--to-stdout" in flags:
                return None
            return _rebase_shell_paths(directories or ["."], cwd)
        if cmd == "pax":
            if "r" not in flags or "w" in flags:
                return None
            return _rebase_shell_paths(["."], cwd)
        if not flags.isdisjoint({"x", "--extract"}):
            return _rebase_shell_paths(directories or ["."], cwd)
    return None


def classify_archive_command(
    cmd: str, parts: list[str], cwd: str | None
) -> _ShellSegmentMetadata | None:
    unknown = False
    if cmd in {"tar", "bsdtar", "gtar"}:
        extracts, paths, unknown = _tar_extract_paths(parts, cwd)
        if not extracts:
            return None
    else:
        destinations = _other_archive_destinations(cmd, parts, cwd)
        if destinations is None:
            return None
        paths = destinations
        # cpio/pax can restore absolute member names regardless of cwd. Their
        # option spelling alone does not prove arbitrary archive contents confined.
        unknown = cmd in {"cpio", "pax"}
        options = parts[1 : parts.index("--")] if "--" in parts else parts[1:]
        if cmd == "unzip":
            flags, _ = _archive_options(parts, short_values="dP")
            unknown = ":" in flags  # Explicitly permits parent-directory members.
        elif cmd in {"7z", "7za", "7zz", "7zr"}:
            unknown = any(word.casefold().startswith("-spf") for word in options)
        elif cmd in {"unrar", "rar"}:
            unknown = any(word.casefold().startswith("-ep3") for word in options)
    return _ShellSegmentMetadata(
        "write",
        paths=tuple(paths),
        write_paths=tuple(paths),
        repo_mutation=True,
        extra={
            "canonical_archive_extract_paths": paths,
            **(
                {
                    "_canonical_repo_mutation_scope_unknown": True,
                    "canonical_archive_extract_scope_unknown": True,
                }
                if unknown
                else {}
            ),
        },
    )
