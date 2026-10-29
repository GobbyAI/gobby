"""Bounded patch option and single-diff destination proofs."""

import re
from dataclasses import dataclass
from pathlib import Path

from gobby.hooks._normalization_segments import _ShellSegmentMetadata
from gobby.hooks._normalization_shell import _contains_unexpanded_shell_reference
from gobby.hooks._path_scope import resolve_tool_path
from gobby.hooks.shell_execution import _bounded_read

_PATCH_LIMIT = 64 * 1024
_HUNK = re.compile(r"@@ -[0-9]{1,9}(?:,([0-9]{1,9}))? \+[0-9]{1,9}(?:,([0-9]{1,9}))? @@(?: .*)?\n")
_LONG_VALUES = {
    "--directory": "d",
    "--input": "i",
    "--output": "o",
    "--reject-file": "r",
    "--suffix": "z",
    "--prefix": "B",
    "--version-control": "V",
    "--get": "g",
    "--strip": "p",
    "--fuzz": "F",
    "--ifdef": "D",
    "--debug": "x",
}
_LONG_FLAGS = {
    "--check": "C",
    "--dry-run": "C",
    "--unified": "u",
    "--context": "c",
    "--normal": "n",
    "--force": "f",
    "--batch": "t",
    "--quiet": "s",
    "--silent": "s",
    "--reverse": "R",
    "--forward": "N",
    "--ignore-whitespace": "l",
    "--remove-empty-files": "E",
    "--posix": "posix",
}


@dataclass(frozen=True)
class _PatchOptions:
    flags: frozenset[str]
    values: tuple[tuple[str, str], ...]
    operands: tuple[str, ...]


def _patch_options(arguments: list[str]) -> _PatchOptions | None:
    if any(
        _contains_unexpanded_shell_reference(word) or any(char in word for char in "*?[{}")
        for word in arguments
    ):
        return None
    flags: set[str] = set()
    values: list[tuple[str, str]] = []
    index = 0
    while index < len(arguments):
        word = arguments[index]
        index += 1
        if word == "--":
            break
        if not word.startswith("-") or word == "-":
            index -= 1
            break
        if word.startswith("--"):
            option, separator, value = word.partition("=")
            if option in _LONG_FLAGS and not separator:
                flags.add(_LONG_FLAGS[option])
                continue
            letter = _LONG_VALUES.get(option)
            if letter is None:
                return None
            if not separator:
                if index == len(arguments):
                    return None
                value = arguments[index]
                index += 1
            if not value:
                return None
            values.append((letter, value))
            continue
        for offset, letter in enumerate(word[1:], start=1):
            if letter in "diorzBVgpFDx":
                value = word[offset + 1 :]
                if not value:
                    if index == len(arguments):
                        return None
                    value = arguments[index]
                    index += 1
                if not value:
                    return None
                values.append((letter, value))
                break
            # -b has incompatible optional-argument behavior across patch versions.
            if letter not in "CucnftsRNlE":
                return None
            flags.add(letter)
    return _PatchOptions(frozenset(flags), tuple(values), tuple(arguments[index:]))


def _single_unified_diff(data: bytes) -> bool:
    if not data or len(data) > _PATCH_LIMIT or b"\x00" in data or b"\r" in data:
        return False
    try:
        lines = data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return False
    if len(lines) < 3 or not lines[0].startswith("--- ") or not lines[1].startswith("+++ "):
        return False
    index = 2
    hunks = 0
    while index < len(lines):
        match = _HUNK.fullmatch(lines[index])
        if match is None:
            return False
        old, new = (int(value) if value is not None else 1 for value in match.groups())
        index += 1
        hunks += 1
        while old or new:
            if index == len(lines) or not lines[index].endswith("\n"):
                return False
            marker = lines[index][:1]
            if marker not in {" ", "+", "-"}:
                return False
            old -= marker in {" ", "-"}
            new -= marker in {" ", "+"}
            if old < 0 or new < 0:
                return False
            index += 1
            if lines[index : index + 1] == ["\\ No newline at end of file\n"]:
                index += 1
    return hunks > 0


def classify_patch(
    parts: list[str], cwd: str | None, base_cwd: Path | None
) -> _ShellSegmentMetadata:
    """Scope persistent writes for explicit originals with -g0 and a safe -z suffix.

    The single diff must live outside all destination parents. Unproved input,
    option grammar, backup placement or self-rewrite remains an unknown write.
    """
    unknown = _ShellSegmentMetadata("write", repo_mutation=True)
    options = _patch_options(parts[1:])
    if options is None:
        return unknown
    if "C" in options.flags:
        return _ShellSegmentMetadata("execute", read_only=True)
    # A different input grammar, backup prefix or debug mode invalidates the proof.
    if options.flags - {"u", "f", "t", "s", "R", "N", "l", "E", "posix"}:
        return unknown
    if any(key not in {"d", "i", "o", "r", "z", "V", "g", "p", "F"} for key, _ in options.values):
        return unknown
    values = dict(options.values)
    suffix = values.get("z", "")
    if values.get("g") != "0" or not suffix or any(char in suffix for char in "/\\"):
        return unknown
    # Directory options act in order; other duplicate options stay conservative.
    keys = [key for key, _ in options.values if key != "d"]
    if len(keys) != len(set(keys)):
        return unknown
    operands = options.operands
    if len(operands) == 2 and "i" not in values:
        original, patchfile = operands
    elif len(operands) == 1 and "i" in values:
        original, patchfile = operands[0], values["i"]
    else:
        return unknown
    working = resolve_tool_path(cwd, base_cwd) if cwd else base_cwd
    for key, value in options.values:
        if key == "d":
            working = resolve_tool_path(value, working)
            if working is None:
                return unknown
    if working is None or original == "-" or patchfile == "-":
        return unknown
    output = values.get("o", original)
    reject = values.get("r", output + ".rej")
    targets = [resolve_tool_path(word, working) for word in (original, output, reject)]
    if any(target is None for target in targets) or "-" in (output, reject):
        return unknown
    read = _bounded_read(patchfile, working, _PATCH_LIMIT + 1)
    if read is None or not _single_unified_diff(read[1]):
        return unknown
    # Parent scope includes simple/numbered backups and backups of existing rejects.
    # Include original even with -o: patch can create a missing original first.
    parents = tuple(dict.fromkeys(str(target.parent) for target in targets if target is not None))
    if any(read[0].is_relative_to(parent) for parent in parents):
        return unknown
    return _ShellSegmentMetadata(
        "write",
        paths=parents,
        write_paths=parents,
        repo_mutation=True,
        extra={"_canonical_patch_input_paths": [str(read[0])]},
    )
