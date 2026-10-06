"""Test selection and lexical coverage of Python tests and web sources at close."""

from __future__ import annotations

import ast
import filecmp
import logging
import os
import posixpath
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from gobby.config.shell_lexing import parse_shell_command, safe_split
from gobby.tasks.command_equivalence import pytest_targets, run_location, vitest_related_targets
from gobby.tasks.related_tests import RELATED_TEST_MAX_FILES
from gobby.tasks.transcript_evidence_models import TranscriptValidationRun

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


def _module_imports(
    path: Path,
    module: str,
    relevant_prefixes: set[str] | None = None,
    parent_import: re.Pattern[str] | None = None,
    *,
    text: str | None = None,
) -> set[str]:
    """Read imports without importing/executing repository modules."""
    try:
        if text is None:
            text = path.read_text(encoding="utf-8")
        if (
            relevant_prefixes is not None
            and not any(prefix in text for prefix in relevant_prefixes)
            and not (
                parent_import is not None
                and parent_import.search(text)
                and any(prefix.rpartition(".")[2] in text for prefix in relevant_prefixes)
            )
        ):
            return set()
        tree = ast.parse(text)
    except (OSError, UnicodeError, SyntaxError) as exc:
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
    parsed_siblings: dict[Path, set[str]] = {}
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
                    parsed_siblings[path] = _module_imports(path, sibling, leaves, text=text)
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
    candidates = [
        base / path
        for path in pytest_module_paths(
            path.relative_to(base).as_posix()
            for path in (base / "tests").rglob("*.py")
            if path.is_file()
        )
    ]
    tests = [
        (
            path.relative_to(base).as_posix(),
            _module_imports(
                path, _module_name(path.relative_to(base).as_posix()), prefixes, parent_import
            ),
        )
        for path in candidates
    ]
    for source, family in families.items():
        source_path = PurePosixPath(source)
        module = _module_name(source)
        package_parts = module.split(".")[:-1]
        if package_parts and package_parts[0] == "gobby":
            package_parts.pop(0)
        mirror_parent = PurePosixPath("tests", *package_parts)
        ranked: list[tuple[int, str]] = []
        for test, imports in tests:
            test_path = PurePosixPath(test)
            ranks = [2 * family[imported] + 1 for imported in imports.intersection(family)]
            if test_path.parent == mirror_parent:
                ranks.extend(
                    2 * distance
                    for member, distance in family.items()
                    if (stem := member.rpartition(".")[2].lstrip("_"))
                    and (test_path.stem == f"{stem}_test" or test_path.stem == f"test_{stem}")
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
) -> tuple[str, ...]:
    """Return changed tests no successful pytest run targets.

    Without ``close_root`` targets match lexically. With it, each target resolves from
    the run's location: a target in ``close_root`` covers as before, and one in another
    tree covers only when that tree holds the root's bytes for the test and every
    changed path (#23653).
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
        if not any(_runs_test(target, test, root, changed_paths) for target in resolved)
    )


def identical_copy_run(
    run: TranscriptValidationRun,
    changed_python_tests: tuple[str, ...],
    close_root: str,
    changed_paths: Sequence[str],
) -> bool:
    """Whether a passing pytest run targets only identical copies of changed tests.

    Such a run proved this task's test bytes from another tree, so a ``uv`` location
    outside ``close_root`` does not make it foreign (#23653).
    """
    root = os.path.realpath(close_root)
    targets = _resolved_pytest_targets(run, root) if run.outcome == "success" else ()
    return bool(targets) and all(
        any(
            _runs_test(target, test, root, changed_paths, copy_only=True)
            for test in changed_python_tests
        )
        for target in targets
    )


def copy_differing_paths(
    runs: Iterable[TranscriptValidationRun],
    tests: Sequence[str],
    close_root: str | None,
    changed_paths: Sequence[str],
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
        if not _same_bytes(tree, root, path)
    }
    return tuple(sorted(differing))


def _resolved_pytest_targets(run: TranscriptValidationRun, root: str) -> tuple[str, ...]:
    """Return the run's pytest targets as absolute paths, resolved from where it ran."""
    targets = pytest_targets(run.core_command or run.command)
    location = run_location(run.command, workdir=run.workdir)
    if not targets or location is None:
        return ()
    paths = (os.path.normpath(os.path.join(root, location, target)) for target in targets)
    return tuple(_canonical_target(path, root) for path in paths)


def _canonical_target(path: str, root: str) -> str:
    """Canonicalize ``path`` so that no symlink into ``root`` poses as a copy.

    A target that resolves into ``root`` is the root's own file. Any other keeps its file
    name under a canonical parent, so a copy held as a file symlink compares by its bytes.
    """
    resolved = os.path.realpath(path)
    if resolved == root or resolved.startswith(f"{root}{os.sep}"):
        return resolved
    return os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))


def _runs_test(
    target: str,
    test: str,
    root: str,
    changed_paths: Sequence[str],
    *,
    copy_only: bool = False,
) -> bool:
    """Whether absolute ``target`` runs ``test`` at ``root`` or as an identical copy.

    A tree other than ``root`` must hold the root's bytes for ``test`` and every changed
    path, or it tests other code; equal bytes are git blob identity for an unfiltered file.
    """
    return any(
        not copy_only
        if tree == root
        else all(_same_bytes(tree, root, path) for path in (test, *changed_paths))
        for tree in _target_trees(target, test)
    )


def _target_trees(target: str, test: str) -> Iterator[str]:
    """Yield each tree ``R`` whose ``R/q`` is ``target``, for ``test`` or a parent ``q``."""
    for prefix in (test, *(parent.as_posix() for parent in PurePosixPath(test).parents)):
        if prefix == ".":
            yield target
        elif target.endswith(f"/{prefix}"):
            yield target.removesuffix(f"/{prefix}")


def _same_bytes(tree: str, root: str, path: str) -> bool:
    """Whether ``path`` holds the same bytes in both trees, or is absent from both."""
    copy, original = os.path.join(tree, path), os.path.join(root, path)
    try:
        return filecmp.cmp(copy, original, shallow=False)
    except OSError:
        return not os.path.exists(copy) and not os.path.exists(original)


def coverage_failure_message(
    python_tests: tuple[str, ...],
    python_sources: Mapping[str, Sequence[str]],
    web_paths: tuple[str, ...],
    *,
    differing_paths: tuple[str, ...] = (),
) -> str | None:
    """Describe the first uncovered test obligation in checklist priority order."""
    copies = (
        " A run from another tree is credited only when that tree matches the close "
        f"checkout; these paths differ: {', '.join(f'`{path}`' for path in differing_paths)}."
        if differing_paths
        else ""
    )
    if python_tests:
        display = ", ".join(f"`{path}`" for path in python_tests)
        return (
            "Changed Python tests have no credited fresh passing pytest target. "
            f"Uncovered paths: {display}.{copies}"
        )
    if python_sources:
        display = "; ".join(
            f"`{source}`: " + ", ".join(f"`{test}`" for test in tests)
            for source, tests in python_sources.items()
        )
        return (
            "Changed Python sources have related tests with no credited fresh passing pytest target. "
            f"Uncovered sources and tests: {display}.{copies}"
        )
    if web_paths:
        # Direct binary avoids wrappers that rewrite `vitest related` into `vitest run`.
        display = ", ".join(f"`{path}`" for path in web_paths)
        return (
            "Changed web/src files have no credited fresh passing `vitest related` run. "
            f"Uncovered paths: {display}. Run `cd web && node_modules/.bin/vitest "
            "related <each path relative to web/> --run` clean after the final task edit; "
            "add `--passWithNoTests` when a path has no runtime importer (type-only "
            "modules, declarations, assets)."
        )
    return None


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
) -> tuple[str, ...]:
    """Return changed web paths no successful ``vitest related`` run names.

    ``vitest related`` takes files, so coverage is an exact path match. The run's
    tool workdir and leading ``cd`` chain must place it in ``web/``.
    """
    covered: set[str] = set()
    for run in runs:
        targets = vitest_related_targets(run.command, close_root=close_root, workdir=run.workdir)
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
