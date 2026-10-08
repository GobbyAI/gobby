"""Test selection and lexical coverage of Python tests and web sources at close."""

from __future__ import annotations

import ast
import filecmp
import hashlib
import io
import logging
import os
import posixpath
import re
import shlex
import stat
import threading
import tokenize
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from functools import cached_property, lru_cache
from pathlib import Path, PurePosixPath

from gobby.config.shell_lexing import parse_shell_command, safe_split
from gobby.tasks.command_equivalence import (
    pytest_targets,
    run_location,
    runs_outside_root,
    vitest_related_targets,
)
from gobby.tasks.related_tests import RELATED_TEST_MAX_FILES
from gobby.tasks.transcript_evidence_models import TranscriptEvidence, TranscriptValidationRun
from gobby.utils.git import run_git_command

_TEST_TYPES_AUDIT_MATCHER = "gobby-test-types-audit"
logger = logging.getLogger(__name__)
_TEST_TYPES_BASELINE = ".gobby/test-types-baseline.json"
# Flags that change only how the audit reports. ``--min-severity`` cannot weaken the
# ratchet: its default is already the loosest choice, so an explicit value is at least
# as strict. Everything else -- ``--write-baseline``, ``--allow-failing-baseline``,
# ``--mypy-command`` -- changes what the audit enforces and still voids credit.
_TEST_TYPES_OUTPUT_ONLY_FLAGS = frozenset({"--format", "--output", "--min-severity"})


def changed_python_test_paths(changed_paths: Iterable[str]) -> tuple[str, ...]:
    python_tests: set[str] = set()
    for path in changed_paths:
        normalized = _normalize_repo_path(path)
        if (
            normalized is not None
            and normalized.startswith("tests/")
            and normalized.endswith(".py")
        ):
            python_tests.add(normalized)
    return tuple(sorted(python_tests))


def changed_python_source_paths(changed_paths: Iterable[str]) -> tuple[str, ...]:
    """Pure source-path selection, safe for the rootless event-loop probe."""
    return tuple(
        sorted(
            {
                normalized
                for path in changed_paths
                if (normalized := _normalize_repo_path(path)) is not None
                and normalized.endswith(".py")
                and not normalized.startswith("tests/")
            }
        )
    )


def _module_name(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts.pop(0)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _module_imports(path: Path, module: str, text: str) -> set[str]:
    """Read imports without importing/executing repository modules."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        logger.debug("Cannot select related tests from %s: %s", path, exc)
        return set()
    package = module if path.stem == "__init__" else module.rpartition(".")[0]
    imports: set[str] = set()
    pending: list[ast.AST] = [tree]
    while pending:
        node = pending.pop()
        # Imports only occur in statement bodies; skip expression subtrees.
        pending.extend(
            child
            for child in ast.iter_child_nodes(node)
            if isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case))
        )
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = node.module or ""
            if node.level:
                parts = package.split(".") if package else []
                prefix = ".".join(
                    parts[: len(parts) - node.level + 1] + ([prefix] if prefix else [])
                )
            if prefix:
                imports.add(prefix)
                imports.update(
                    f"{prefix}.{alias.name}" for alias in node.names if alias.name != "*"
                )
    return imports


# Parsed imports by module, package-ness and content digest, the parse's only inputs,
# so the same test in every worktree shares one entry; path keys made each of the many
# checkouts evict the others' parses (#23359). The least recently used entries past
# the cap go, so deleted tests cannot grow it for the daemon's lifetime. Closes
# evaluate on worker threads.
_PARSED_IMPORTS_MAX = 8192
_PARSED_IMPORTS: OrderedDict[tuple[str, bool, bytes], frozenset[str]] = OrderedDict()
_PARSED_IMPORTS_LOCK = threading.Lock()


def _parsed_imports(path: Path, module: str, text: str) -> frozenset[str]:
    key = (module, path.stem == "__init__", hashlib.blake2b(text.encode(), digest_size=16).digest())
    with _PARSED_IMPORTS_LOCK:
        cached = _PARSED_IMPORTS.get(key)
        if cached is not None:
            _PARSED_IMPORTS.move_to_end(key)
            return cached
    imports = frozenset(_module_imports(path, module, text))
    with _PARSED_IMPORTS_LOCK:
        _PARSED_IMPORTS[key] = imports
        _PARSED_IMPORTS.move_to_end(key)
        while len(_PARSED_IMPORTS) > _PARSED_IMPORTS_MAX:
            _PARSED_IMPORTS.popitem(last=False)
    return imports


_IMPORT_KEYWORD = re.compile(r"\b(?:from|import)\b")
_IMPORT_MODULE = re.compile(r"(?m)^[ \t]*(?:from|import)[ \t]+([\w.]+)")


def _import_header_mentions(text: str, prefixes: set[str], leaves: set[str]) -> bool:
    """Conservatively reject body-only mentions before building a full-file AST.

    Start at each possible import keyword, including inline/nested statements.
    Tokenize only its logical line, preserving parentheses and continuations.
    Matches in strings/comments may admit an extra parse; uncertain tokenization
    always falls back to the existing parser rather than losing a related test.
    """
    # Most real consumers use a simple absolute import; admit those in C without
    # tokenizing the other imports in a large test file.
    if any(match[1] in prefixes for match in _IMPORT_MODULE.finditer(text)):
        return True
    reader = io.StringIO(text)
    for match in _IMPORT_KEYWORD.finditer(text):
        reader.seek(match.start())
        try:
            for token in tokenize.generate_tokens(reader.readline):
                if token.type in (tokenize.NEWLINE, tokenize.ENDMARKER) or token.string == ";":
                    break
                if token.type == tokenize.NAME and (
                    token.string in leaves or not token.string.isascii()
                ):
                    return True
        except (SyntaxError, tokenize.TokenError):
            return True
    return False


def _test_imports(
    path: Path,
    module: str,
    prefixes: set[str],
    leaves: set[str],
    parent_import: re.Pattern[str] | None,
) -> frozenset[str]:
    """Imports of a test that may import a prefix; every prefix contains its leaf."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        logger.debug("Cannot select related tests from %s: %s", path, exc)
        return frozenset()
    if not any(leaf in text for leaf in leaves) or not (
        any(prefix in text for prefix in prefixes)
        or (parent_import is not None and parent_import.search(text) is not None)
    ):
        return frozenset()
    if not _import_header_mentions(text, prefixes, leaves):
        return frozenset()
    return _parsed_imports(path, module, text)


# A test tree's imports per prefix set, reused per file while its stat matches (#23359):
# close retries rescan the same tree for the same changed modules. The least recently
# used prefix sets past the cap go.
_TEST_SCANS_MAX = 32
_TEST_SCANS: OrderedDict[
    tuple[Path, frozenset[str]], dict[str, tuple[tuple[int, ...] | None, frozenset[str]]]
] = OrderedDict()
_TEST_SCANS_LOCK = threading.Lock()


def _scan_tests(
    base: Path, prefixes: set[str], leaves: set[str], parent_import: re.Pattern[str] | None
) -> list[tuple[str, frozenset[str]]]:
    """Each collected test module with ``_test_imports``, read only when it changed."""
    tests_dir = base / "tests"
    # os.walk joins onto tests_dir, so slicing its string skips relative_to; like rglob,
    # it lists symlinked files but never descends into symlinked directories.
    offset = len(str(tests_dir)) - len("tests")
    signatures: dict[str, tuple[int, ...]] = {}
    for root, _, names in os.walk(tests_dir):
        for name in names:
            if not name.endswith(".py"):
                continue
            try:
                info = os.stat(os.path.join(root, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                signatures[f"{root[offset:]}/{name}"] = (
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
    key = (tests_dir, frozenset(prefixes))
    with _TEST_SCANS_LOCK:
        previous = _TEST_SCANS.get(key, {})
    scan: dict[str, tuple[tuple[int, ...] | None, frozenset[str]]] = {}
    for test in pytest_module_paths(signatures):
        signature = signatures.get(test)
        entry = previous.get(test)
        if signature is None or entry is None or entry[0] != signature:
            entry = (
                signature,
                _test_imports(base / test, _module_name(test), prefixes, leaves, parent_import),
            )
        scan[test] = entry
    with _TEST_SCANS_LOCK:
        _TEST_SCANS[key] = scan
        _TEST_SCANS.move_to_end(key)
        while len(_TEST_SCANS) > _TEST_SCANS_MAX:
            _TEST_SCANS.popitem(last=False)
    return [(test, imports) for test, (_, imports) in scan.items()]


def related_python_source_tests(
    changed_paths: Iterable[str], base_dir: str | Path
) -> dict[str, tuple[str, ...]]:
    """Select exact imports and mirrored package filenames in one test-tree scan.

    Sibling facade modules importing a changed module count as related, including
    relative imports. Generic filenames never match unrelated package names.
    Call only in the rooted, off-thread close evaluation.
    """
    base = Path(base_dir)
    sources = changed_python_source_paths(changed_paths)
    selected: dict[str, tuple[str, ...]] = dict.fromkeys(sources, ())
    if not sources or not (base / "tests").is_dir():
        return selected
    packages: dict[Path, dict[str, tuple[Path, str]]] = {}
    parsed_siblings: dict[Path, frozenset[str]] = {}
    families: dict[str, dict[str, int]] = {}
    for source in sources:
        source_path = PurePosixPath(source)
        if source_path.stem == "__init__":
            continue  # Package initializers have no unambiguous module-owned filename.
        module = _module_name(source)
        parent = base / source_path.parent
        if parent not in packages:
            packages[parent] = {}
            for path in parent.glob("*.py"):
                if not path.is_file() or path.stem == "__init__":
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    logger.debug("Cannot select related tests from %s: %s", path, exc)
                    continue
                packages[parent][_module_name(path.relative_to(base).as_posix())] = (path, text)
        family = {module: 0}
        while True:
            leaves = {member.rpartition(".")[2] for member in family}
            importers: dict[str, int] = {}
            for sibling, (path, text) in packages[parent].items():
                if sibling in family or not any(leaf in text for leaf in leaves):
                    continue
                if path not in parsed_siblings:
                    parsed_siblings[path] = _parsed_imports(path, sibling, text)
                if dependencies := parsed_siblings[path].intersection(family):
                    importers[sibling] = 1 + min(family[dependency] for dependency in dependencies)
            if not importers:
                break
            family.update(importers)
        families[source] = family
    prefixes = {module for family in families.values() for module in family}
    parents = {parent_module for module in prefixes if (parent_module := module.rpartition(".")[0])}
    parent_import = (
        re.compile(
            r"(?m)^[ \t]*from[ \t]+(?:"
            + "|".join(re.escape(parent) for parent in sorted(parents))
            + r")[ \t]+import\b"
        )
        if parents
        else None
    )
    leaves = {prefix.rpartition(".")[2] for prefix in prefixes}
    tests = _scan_tests(base, prefixes, leaves, parent_import)
    for source, family in families.items():
        package_parts = _module_name(source).split(".")[:-1]
        if package_parts and package_parts[0] == "gobby":
            package_parts.pop(0)
        # Test paths are normalized "tests/..." strings ending in ".py", so string parts
        # match their pathlib parent and stem without a path object per test (#23359).
        mirror_parent = "/".join(("tests", *package_parts))
        ranked: list[tuple[int, str]] = []
        for test, imports in tests:
            ranks = [2 * family[imported] + 1 for imported in imports.intersection(family)]
            test_parent, _, test_name = test.rpartition("/")
            if test_parent == mirror_parent:
                test_stem = test_name.removesuffix(".py")
                ranks.extend(
                    2 * distance
                    for member, distance in family.items()
                    if (stem := member.rpartition(".")[2].lstrip("_"))
                    and (test_stem == f"{stem}_test" or test_stem == f"test_{stem}")
                )
            if ranks:
                ranked.append((min(ranks), test))
        selected[source] = tuple(test for _, test in sorted(ranked)[:RELATED_TEST_MAX_FILES])
    return selected


def pytest_module_paths(changed_paths: Iterable[str]) -> tuple[str, ...]:
    """Select modules collected by this repository's ``python_files`` patterns."""
    return tuple(
        path
        for path in changed_python_test_paths(changed_paths)
        if any(
            fnmatchcase(PurePosixPath(path).name, pattern)
            for pattern in ("test_*.py", "run_*_sandbox.py")
        )
    )


def test_types_audit_targets(run: TranscriptValidationRun) -> tuple[str, ...] | None:
    if run.matcher_id != _TEST_TYPES_AUDIT_MATCHER:
        return None
    core_command = run.core_command or run.command
    if len(parse_shell_command(core_command).segments) != 1:
        return None
    commands = [segment.command for segment in run.validation_segments]
    if not commands:
        commands = [core_command]
    for command in commands:
        targets = _test_types_command_targets(command)
        if targets is not None:
            return targets
    return None


def uncovered_test_paths(
    changed_python_tests: tuple[str, ...],
    audit_targets: tuple[str, ...],
) -> tuple[str, ...]:
    """Return changed tests outside every lexical target, including missing paths."""
    return tuple(
        path
        for path in changed_python_tests
        if not any(
            target == "." or path == target or path.startswith(f"{target}/")
            for target in audit_targets
        )
    )


def uncovered_pytest_paths(
    runs: Iterable[TranscriptValidationRun],
    changed_python_tests: tuple[str, ...],
    *,
    close_root: str | None = None,
    changed_paths: Sequence[str] = (),
    candidate: CloseCandidate | None = None,
) -> tuple[str, ...]:
    """Return changed tests no successful pytest run targets.

    Without ``close_root`` targets match lexically. With it, each target resolves from
    the run's location: a target in ``close_root`` covers as before, and one in another
    tree covers only when that tree holds the close ``candidate`` commit's bytes for the
    test and every changed path (#23653).
    """
    if close_root is None:
        covered: list[str] = []
        for run in runs:
            covered.extend(pytest_targets(run.core_command or run.command) or ())
        return uncovered_test_paths(changed_python_tests, tuple(covered))
    root = os.path.realpath(close_root)
    resolved = [target for run in runs for target in _resolved_pytest_targets(run, root)]
    return tuple(
        test
        for test in changed_python_tests
        if not any(_runs_test(target, test, root, changed_paths, candidate) for target in resolved)
    )


def drop_foreign_runs(
    evidence: TranscriptEvidence,
    changed_python_tests: tuple[str, ...],
    close_root: str,
    changed_paths: Sequence[str],
    candidate: CloseCandidate | None = None,
) -> tuple[TranscriptEvidence, list[str]]:
    """Drop runs that validated another checkout and return their commands.

    A run whose ``uv`` location points outside ``close_root`` neither credits nor fails
    this task, unless it ran identical copies of the changed tests.
    """
    foreign: list[str] = []

    def in_scope(run: TranscriptValidationRun) -> bool:
        if runs_outside_root(run.core_command or run.command, close_root) and not (
            identical_copy_run(run, changed_python_tests, close_root, changed_paths, candidate)
        ):
            foreign.append(run.command)
            return False
        return True

    scoped = replace(
        evidence,
        validation_runs=tuple(filter(in_scope, evidence.validation_runs)),
        command_runs=tuple(filter(in_scope, evidence.command_runs)),
    )
    return scoped, foreign


def identical_copy_run(
    run: TranscriptValidationRun,
    changed_python_tests: tuple[str, ...],
    close_root: str,
    changed_paths: Sequence[str],
    candidate: CloseCandidate | None = None,
) -> bool:
    """Whether a passing pytest run targets only identical copies of changed tests.

    Such a run proved this task's test bytes from another tree, so a ``uv`` location
    outside ``close_root`` does not make it foreign (#23653).
    """
    root = os.path.realpath(close_root)
    targets = _resolved_pytest_targets(run, root) if run.outcome == "success" else ()
    return bool(targets) and all(
        any(
            _runs_test(target, test, root, changed_paths, candidate, copy_only=True)
            for test in changed_python_tests
        )
        for target in targets
    )


def copy_differing_paths(
    runs: Iterable[TranscriptValidationRun],
    tests: Sequence[str],
    close_root: str | None,
    changed_paths: Sequence[str],
    candidate: CloseCandidate | None = None,
) -> tuple[str, ...]:
    """Return the paths whose bytes kept another tree's run of ``tests`` from crediting.

    Only a tree holding a copy of the test counts; any other ran none of it.
    """
    if close_root is None or not tests:
        return ()
    root = os.path.realpath(close_root)
    targets = [target for run in runs for target in _resolved_pytest_targets(run, root)]
    differing = {
        path
        for target in targets
        for test in tests
        for tree in _target_trees(target, test)
        if tree != root and os.path.isfile(os.path.join(tree, test))
        for path in (test, *changed_paths)
        if not _same_bytes(tree, root, path, candidate)
    }
    return tuple(sorted(differing))


def _resolved_pytest_targets(run: TranscriptValidationRun, root: str) -> tuple[str, ...]:
    """Return the run's pytest targets as absolute paths, resolved from where it ran."""
    targets = pytest_targets(run.core_command or run.command)
    location = run_location(run.command, workdir=run.workdir)
    if not targets or location is None:
        return ()
    # Symlinks stay unresolved: pytest's rootdir and pythonpath follow the path as written,
    # so a test symlinked into ``root`` from another tree runs that tree's code.
    return tuple(os.path.normpath(os.path.join(root, location, target)) for target in targets)


def _runs_test(
    target: str,
    test: str,
    root: str,
    changed_paths: Sequence[str],
    candidate: CloseCandidate | None,
    *,
    copy_only: bool = False,
) -> bool:
    """Whether absolute ``target`` runs ``test`` at ``root`` or as an identical copy.

    A tree other than ``root`` must hold the candidate's bytes for ``test`` and every
    changed path, or it tests other code.
    """
    return any(
        not copy_only
        if tree == root
        else all(_same_bytes(tree, root, path, candidate) for path in (test, *changed_paths))
        for tree in _target_trees(target, test)
    )


def _target_trees(target: str, test: str) -> Iterator[str]:
    """Yield each tree ``R`` whose ``R/q`` is ``target``, for ``test`` or a parent ``q``."""
    for prefix in (test, *(parent.as_posix() for parent in PurePosixPath(test).parents)):
        if prefix == ".":
            yield target
        elif target.endswith(f"/{prefix}"):
            yield target.removesuffix(f"/{prefix}")


def _same_bytes(tree: str, root: str, path: str, candidate: CloseCandidate | None) -> bool:
    """Whether ``tree`` holds ``path`` as the close candidate commit does, or both lack it.

    The candidate is the reference, so another session's uncommitted edit in ``root``
    does not void a copy, and a candidate git cannot list matches nothing. Without a
    candidate ``root``'s working tree is the reference. Equal bytes are git blob
    identity for an unfiltered file; a path committed as a symlink or submodule never
    matches, which refuses credit and never grants it.
    """
    copy = os.path.join(tree, path)
    if not candidate:
        original = os.path.join(root, path)
        try:
            return filecmp.cmp(copy, original, shallow=False)
        except OSError:
            return not os.path.exists(copy) and not os.path.exists(original)
    blobs = candidate.blobs
    if blobs is None:
        return False
    blob = blobs.get(path)
    try:
        data = Path(copy).read_bytes()
    except OSError:
        return blob is None and not os.path.exists(copy)
    algorithm = "sha256" if blob is not None and len(blob) == 64 else "sha1"
    object_id = hashlib.new(algorithm, b"blob %d\0" % len(data) + data, usedforsecurity=False)
    return object_id.hexdigest() == blob


@dataclass
class CloseCandidate:
    """The close candidate commit as one gate evaluation compares copies against it.

    A listed commit is immutable, so its listing is shared across evaluations. A failed
    listing is remembered only by this evaluation: its paths fail closed after one git
    timeout, and the next evaluation asks git again.
    """

    root: str
    commit: str

    @cached_property
    def blobs(self) -> Mapping[str, str] | None:
        """Blob ids by repo path in the commit's tree, or None when git cannot list it."""
        try:
            return _candidate_blobs(self.root, self.commit)
        except _UnlistedCommitError:
            return None


class _UnlistedCommitError(Exception):
    """Git could not list the close candidate commit's tree."""


_REGULAR_FILE_MODES = frozenset({"100644", "100755"})


@lru_cache(maxsize=4)
def _candidate_blobs(root: str, commit: str) -> Mapping[str, str]:
    """Return blob ids by repo path in ``commit``'s tree.

    A symlink's blob is its target text and a submodule's id names a commit, so any
    entry that is not a regular file maps to ``""``, which no file digest equals.
    A failure raises, which ``lru_cache`` never stores.
    """
    listing = run_git_command(
        ["git", "ls-tree", "-r", "-z", "--full-tree", commit], cwd=root, timeout=30
    )
    if listing is None:
        raise _UnlistedCommitError(commit)
    blobs: dict[str, str] = {}
    for entry in listing.split("\0"):
        meta, tab, path = entry.partition("\t")
        if tab:
            mode, _kind, object_id = meta.split(" ")
            blobs[path] = object_id if mode in _REGULAR_FILE_MODES else ""
    return blobs


def coverage_failure_messages(
    python_tests: tuple[str, ...],
    python_sources: Mapping[str, Sequence[str]],
    web_paths: tuple[str, ...],
    *,
    differing_paths: tuple[str, ...] = (),
    close_root: str | None = None,
) -> list[tuple[str, str]]:
    """Name and describe every uncovered test obligation in checklist priority order.

    The differing-copy note closes the last pytest obligation, since its paths can
    come from either one.
    """
    failures: list[tuple[str, str]] = []
    if python_tests:
        display = ", ".join(f"`{path}`" for path in python_tests)
        failures.append(
            (
                "pytest_changed_tests",
                "Changed Python tests have no credited fresh passing pytest target. "
                f"Uncovered paths: {display}.",
            )
        )
    if python_sources:
        display = "; ".join(
            f"`{source}`: " + ", ".join(f"`{test}`" for test in tests)
            for source, tests in python_sources.items()
        )
        failures.append(
            (
                "pytest_related_source_tests",
                "Changed Python sources have related tests with no credited fresh passing "
                f"pytest target. Uncovered sources and tests: {display}.",
            )
        )
    if failures and differing_paths:
        name, message = failures[-1]
        failures[-1] = (
            name,
            f"{message} A run from another tree is credited only when that tree matches the "
            "close candidate commit, or the close checkout without one; these paths differ: "
            f"{', '.join(f'`{path}`' for path in differing_paths)}.",
        )
    if web_paths:
        # Direct binary avoids wrappers that rewrite `vitest related` into `vitest run`.
        display = ", ".join(f"`{path}`" for path in web_paths)
        workdir = shlex.quote(os.path.join(close_root, "web")) if close_root else "web"
        failures.append(
            (
                "vitest_related",
                "Changed web/src files have no credited fresh passing `vitest related` run. "
                f"Uncovered paths: {display}. Run `cd {workdir} && node_modules/.bin/vitest "
                "related <each path relative to web/> --run` clean after the final task edit; "
                "add `--passWithNoTests` when a path has no runtime importer (type-only "
                "modules, declarations, assets).",
            )
        )
    return failures


def changed_web_source_paths(changed_paths: Iterable[str]) -> tuple[str, ...]:
    """Select changed files under ``web/src``, sources and tests alike."""
    web_sources = {
        normalized
        for path in changed_paths
        if (normalized := _normalize_repo_path(path)) is not None
        and normalized.startswith("web/src/")
    }
    return tuple(sorted(web_sources))


def uncovered_vitest_related_paths(
    runs: Iterable[TranscriptValidationRun],
    changed_web_paths: tuple[str, ...],
    *,
    close_root: str | None,
    changed_paths: Sequence[str] = (),
    candidate: CloseCandidate | None = None,
) -> tuple[str, ...]:
    """Return changed web paths no successful ``vitest related`` run names.

    ``vitest related`` takes files, so coverage is an exact path match. The run's
    tool workdir and leading ``cd`` chain must place it in ``web/``. Another
    checkout credits only when its targets and every changed path match the
    candidate, using the same byte comparison as pytest copies.
    """
    covered: set[str] = set()
    for run in runs:
        targets = vitest_related_targets(run.command, close_root=close_root, workdir=run.workdir)
        if targets is None and close_root is not None:
            location = run_location(run.command, workdir=run.workdir)
            if location is None or not os.path.isabs(location):
                continue
            web = Path(location).resolve()
            if web.name != "web":
                continue
            tree = str(web.parent)
            targets = vitest_related_targets(run.command, close_root=tree, workdir=run.workdir)
            if targets and not all(os.path.isfile(os.path.join(tree, path)) for path in targets):
                continue
            if targets and not all(
                _same_bytes(tree, close_root, path, candidate)
                for path in (*targets, *changed_paths)
            ):
                continue
        covered.update(targets or ())
    return tuple(path for path in changed_web_paths if path not in covered)


def _test_types_command_targets(command: str) -> tuple[str, ...] | None:
    tokens = safe_split(command)
    prefix = ["gobby", "test-types", "audit"]
    try:
        start = next(
            index
            for index in range(len(tokens) - len(prefix) + 1)
            if tokens[index : index + len(prefix)] == prefix
        )
    except StopIteration:
        return None

    arguments = tokens[start + len(prefix) :]
    targets: list[str] = []
    baselines: list[str] = []
    fail_on_new = 0
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--baseline":
            index += 1
            if index >= len(arguments):
                return None
            baselines.append(arguments[index])
        elif argument.startswith("--baseline="):
            baselines.append(argument.partition("=")[2])
        elif argument == "--fail-on-new":
            fail_on_new += 1
        elif argument in _TEST_TYPES_OUTPUT_ONLY_FLAGS:
            # Consume the flag's value so it is never mistaken for a target.
            index += 1
            if index >= len(arguments):
                return None
        elif argument.partition("=")[0] in _TEST_TYPES_OUTPUT_ONLY_FLAGS:
            pass  # `--flag=value` carries its value inline.
        elif argument.startswith("-"):
            return None
        else:
            targets.append(argument)
        index += 1
    if baselines != [_TEST_TYPES_BASELINE] or fail_on_new != 1 or not targets:
        return None
    normalized_targets: list[str] = []
    for target in targets:
        normalized = _normalize_repo_path(target)
        if normalized is None:
            return None
        normalized_targets.append(normalized)
    return tuple(normalized_targets)


def _normalize_repo_path(path: str) -> str | None:
    normalized = posixpath.normpath(path.replace("\\", "/"))
    if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
        return None
    return normalized
