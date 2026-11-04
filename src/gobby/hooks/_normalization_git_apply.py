"""Git apply scope: inspection and index modes, or the files a literal saved patch names.

A session sets its own edits aside with `git diff -- <paths> > <patch>` and
`git checkout -- <paths>`, then restores them with `git apply <patch>`. Resolving
the patch's targets lets the dirty-path ownership guard refuse foreign files.
Anything this module cannot prove stays an unknown-scope write.
"""

from pathlib import Path

from gobby.hooks import checkout_reads
from gobby.hooks._normalization_segments import _ShellSegmentMetadata
from gobby.hooks._normalization_shell import (
    _contains_unexpanded_shell_reference,
    _rebase_shell_path,
)

_VALUE_OPTIONS = frozenset(
    {"--include", "--exclude", "--directory", "--whitespace", "--build-fake-ancestor", "-p", "-C"}
)
_SWITCHES = frozenset(
    {
        "--stat",
        "--numstat",
        "--summary",
        "--check",
        "--index",
        "--cached",
        "--apply",
        "--3way",
        "--ours",
        "--theirs",
        "--union",
        "--intent-to-add",
        "--unsafe-paths",
        "--allow-empty",
        "--no-add",
        "--reverse",
        "--reject",
        "--binary",
        "--allow-binary-replacement",
        "--inaccurate-eof",
        "--recount",
        "--ignore-space-change",
        "--ignore-whitespace",
        "--verbose",
        "--quiet",
        "--help",
        "-3",
        "-N",
        "-R",
        "-z",
        "-v",
        "-q",
        "-h",
    }
)
# Options that leave the written files exactly the root-relative paths the patch names.
_TARGET_PRESERVING = frozenset(
    {
        "--index",
        "--3way",
        "-3",
        "--ours",
        "--theirs",
        "--union",
        "--reverse",
        "-R",
        "--whitespace",
        "-C",
        "--recount",
        "--ignore-space-change",
        "--ignore-whitespace",
        "--allow-empty",
        "--no-add",
        "--intent-to-add",
        "-N",
        "--binary",
        "--allow-binary-replacement",
        "--inaccurate-eof",
        "--verbose",
        "-v",
        "--quiet",
        "-q",
    }
)
_MAX_PATCH_BYTES = 16 * 1024 * 1024
_NAMED_LINES = ("rename from ", "rename to ", "copy from ", "copy to ")


def _git_apply_options(arguments: list[str]) -> tuple[set[str], list[str]] | None:
    """Prove apply options and collect patch operands without treating values as flags."""
    if any(
        _contains_unexpanded_shell_reference(argument)
        or any(character in argument for character in "*?[{}")
        for argument in arguments
    ):
        return None
    flags: set[str] = set()
    operands: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument == "--":
            operands.extend(arguments[index:])
            break
        if not argument.startswith("-") or argument == "-":
            operands.append(argument)
            continue
        if argument.startswith("--"):
            option, separator, value = argument.partition("=")
            if option in _VALUE_OPTIONS:
                if not separator:
                    if index == len(arguments):
                        return None
                    value = arguments[index]
                    index += 1
                if not value:
                    return None
            elif option not in _SWITCHES or separator:
                return None
            flags.add(option)
            continue
        for offset, character in enumerate(argument[1:], start=1):
            option = f"-{character}"
            if option in _VALUE_OPTIONS:
                value = argument[offset + 1 :]
                if not value:
                    if index == len(arguments):
                        return None
                    value = arguments[index]
                    index += 1
                if not value.isdecimal():
                    return None
                flags.add(option)
                break
            if option not in _SWITCHES:
                return None
            flags.add(option)
    return flags, operands


def classify_git_apply(
    arguments: list[str], unknown_selector: bool, git_cwd: str | None, root: Path | None
) -> _ShellSegmentMetadata:
    parsed = _git_apply_options(arguments)
    unknown_write = _ShellSegmentMetadata("write", repo_mutation=True)
    if parsed is None or unknown_selector:
        return unknown_write
    flags, operands = parsed
    if flags & {"-h", "--help"}:
        return _ShellSegmentMetadata("execute")
    # This writes a separate index file even in inspection modes. Its destination
    # is deliberately left unproven until it has its own complete scope handling.
    if "--build-fake-ancestor" in flags:
        return unknown_write
    if "--apply" not in flags and flags & {"--check", "--stat", "--numstat", "--summary"}:
        return _ShellSegmentMetadata("execute")
    if "--reject" in flags:
        # Rejected hunks create worktree .rej files even with --cached.
        return unknown_write
    if "--cached" in flags:
        # Like git add/restore --staged: require a task, without claiming file edits.
        return _ShellSegmentMetadata("execute", repo_mutation=True)
    return _literal_patch_write(flags, operands, git_cwd, root) or unknown_write


def _literal_patch_write(
    flags: set[str], operands: list[str], git_cwd: str | None, root: Path | None
) -> _ShellSegmentMetadata | None:
    if (
        root is None
        or not git_cwd
        or not Path(git_cwd).is_absolute()
        or checkout_reads.node_origin()
        or not flags <= _TARGET_PRESERVING
        or not operands
        or any(operand == "-" or operand.startswith("~") for operand in operands)
    ):
        return None
    patches = [Path(_rebase_shell_path(operand, git_cwd)) for operand in operands]
    targets: set[str] = set()
    for patch in patches:
        named = _patch_targets(patch)
        if named is None:
            return None
        targets |= named
    if not targets:
        return None
    # Git resolves patch paths from the checkout root, even when run in a subdirectory.
    return _ShellSegmentMetadata(
        "write",
        paths=tuple(str(root / target) for target in sorted(targets)),
        repo_mutation=True,
        navigation_paths=tuple(str(patch) for patch in patches),
    )


def _patch_targets(patch: Path) -> set[str] | None:
    """Every path the patch can write, or None when any header is unprovable.

    Every `---`/`+++`, rename and copy line counts, even one inside hunk text, so
    a match can only add a path or fail closed.
    """
    try:
        if not patch.is_file() or patch.stat().st_size > _MAX_PATCH_BYTES:
            return None
        text = patch.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return None
    targets: set[str] = set()
    header: str | None = None
    named = True
    for line in [*text.splitlines(), "diff --git "]:
        if line.startswith("diff --git "):
            if not named:
                # Mode-only and binary changes name their file only in the header.
                same = _same_sides(header or "")
                if same is None:
                    return None
                targets.add(same)
            header, named = line.removeprefix("diff --git "), False
            continue
        if line.startswith(("--- ", "+++ ")):
            name = line[4:]
            target = None if name == "/dev/null" else _strip_prefix(name)
            if target is None and name != "/dev/null":
                return None
        elif line.startswith(_NAMED_LINES):
            target = _safe_path(line.split(" ", 2)[2])
            if target is None:
                return None
        else:
            continue
        named = True
        if target is not None:
            targets.add(target)
    return targets


def _same_sides(header: str) -> str | None:
    if len(header) % 2 == 0:
        return None
    half = len(header) // 2
    if header[half] != " ":
        return None
    left, right = _strip_prefix(header[:half]), _strip_prefix(header[half + 1 :])
    return left if left is not None and left == right else None


def _strip_prefix(name: str) -> str | None:
    _, separator, rest = name.partition("/")
    return _safe_path(rest) if separator and not name.startswith('"') else None


def _safe_path(path: str) -> str | None:
    if not path or path.startswith(('"', "/")) or "\t" in path:
        return None
    if any(part in {"", ".", ".."} for part in path.split("/")):
        return None
    return path
