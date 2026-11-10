"""Git apply scope: inspection and index modes, or the files a literal saved patch names.

A session sets its own edits aside with `git diff -- <paths> > <patch>` and
`git checkout -- <paths>`, then restores them with `git apply <patch>`. Resolving
the patch's targets lets the dirty-path ownership guard refuse foreign files.
Anything this module cannot prove stays an unknown-scope write.
"""

from pathlib import Path

from gobby.hooks import checkout_reads
from gobby.hooks._normalization_apply_rewrite import (
    APPLY_READ_PATHS_KEY,
    VALIDATED_APPLY_TEXT,
    apply_rewrite_scope,
    read_validated_patch,
)
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
_NAMED_LINES = ("rename from ", "rename to ", "copy from ", "copy to ")


def _may_become_option(word: str) -> bool:
    """Whether a word's expansion may yield an option such as ``--apply``.

    Any expansion may word-split or carry a leading dash; a glob or brace can only
    produce one when it leads the word, so ``p*.diff`` and ``--include=src/*`` stay
    proven while ``*`` and ``{a,--apply}`` do not.
    """
    return _contains_unexpanded_shell_reference(word) or word[:1] in {"*", "?", "[", "{"}


def _is_literal(word: str) -> bool:
    """Whether a word names exactly itself, with no expansion or pattern."""
    return not _contains_unexpanded_shell_reference(word) and not any(
        character in word for character in "*?[{}"
    )


def _git_apply_options(arguments: list[str]) -> tuple[set[str], list[str]] | None:
    """Prove apply options and collect patch operands without treating values as flags."""
    flags: set[str] = set()
    operands: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument == "--":
            # Operands after ``--`` cannot become options, however they expand.
            operands.extend(arguments[index:])
            break
        if _may_become_option(argument):
            return None
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
                    if _may_become_option(value):
                        return None
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
        # Only a validated literal patch has a known scope: it is rewritten to the bytes
        # checked here. Any other --cached apply lets git read the patch path at run time.
        validated = _validated_patch_text(operands, arguments, git_cwd, root)
        if validated is None:
            return unknown_write
        text, patch = validated
        return _ShellSegmentMetadata(
            "execute",
            repo_mutation=True,
            extra={VALIDATED_APPLY_TEXT: text, APPLY_READ_PATHS_KEY: [str(patch)]},
        )
    return _literal_patch_write(flags, operands, arguments, git_cwd, root) or unknown_write


def _validated_patch_text(
    operands: list[str], arguments: list[str], git_cwd: str | None, root: Path | None
) -> tuple[str, Path] | None:
    """The validated bytes and path of the single literal patch operand, or None unread.

    The file is read only inside a command the Bash normalizer proved to be exactly one
    literal `git apply`, so the bytes the rewrite embeds are the bytes classified here,
    and only from inside the checkout root (see `read_validated_patch`).
    """
    if (
        root is None
        or not apply_rewrite_scope.get()
        or len(operands) != 1
        or arguments[-1:] != operands
        or not git_cwd
        or not Path(git_cwd).is_absolute()
        or operands[0] == "-"
        or operands[0].startswith("~")
        or not _is_literal(operands[0])
    ):
        return None
    patch = Path(_rebase_shell_path(operands[0], git_cwd))
    text = read_validated_patch(root, patch)
    return None if text is None else (text, patch)


def _literal_patch_write(
    flags: set[str],
    operands: list[str],
    arguments: list[str],
    git_cwd: str | None,
    root: Path | None,
) -> _ShellSegmentMetadata | None:
    if root is None or git_cwd is None or checkout_reads.node_origin():
        return None
    if not flags <= _TARGET_PRESERVING:
        return None
    validated = _validated_patch_text(operands, arguments, git_cwd, root)
    if validated is None:
        return None
    text, patch = validated
    targets = _patch_targets_from_text(text)
    if not targets:
        return None
    # Git resolves patch paths from the checkout root, even when run in a subdirectory.
    return _ShellSegmentMetadata(
        "write",
        paths=tuple(str(root / target) for target in sorted(targets)),
        repo_mutation=True,
        navigation_paths=(str(patch),),
        extra={VALIDATED_APPLY_TEXT: text, APPLY_READ_PATHS_KEY: [str(patch)]},
    )


def _patch_targets_from_text(text: str) -> set[str] | None:
    """Every path the patch text can write, or None when any header is unprovable.

    Every `---`/`+++`, rename and copy line counts, even one inside hunk text, so
    a match can only add a path or fail closed.
    """
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
