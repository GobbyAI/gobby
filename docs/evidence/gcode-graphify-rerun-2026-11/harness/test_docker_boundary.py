"""Seat-run harness subcommands must not reach a Docker call site.

Ruling 64fbdf7f: the Orchestrator runs write-compose and provision on the host lane.
A seat runs only the subcommands listed here. The check is static: from each entry it
follows bare-name and attribute references into harness functions, and fails if any
reached function names the string "docker". The write-compose control proves the walk
does reach Docker when Docker is reachable.
"""

from __future__ import annotations

import ast
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
SEAT_RUN_COMMANDS = (
    "init",
    "refresh-manifest",
    "build-probe",
    "write-manifest",
    "materialize-workspaces",
)
HOST_LANE_COMMAND = "write-compose"
SEAT_RUN_FUNCTIONS = (
    ("verify_corpora", "main"),
    ("verify_corpora", "verify_corpora"),
    ("verify_corpora", "compare_manifests"),
    ("binary_set", "install_binary_set"),
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_functions() -> dict[str, list[ast.FunctionDef]]:
    definitions: dict[str, list[ast.FunctionDef]] = {}
    for path in HARNESS.glob("*.py"):
        if path.name.startswith("test_"):
            continue
        for node in _parse(path).body:
            if isinstance(node, ast.FunctionDef):
                definitions.setdefault(node.name, []).append(node)
    return definitions


def _function(module: str, name: str) -> ast.FunctionDef:
    for node in _parse(HARNESS / f"{module}.py").body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{module}.{name} is not a module-level function")


def _referenced_names(nodes: list[ast.AST]) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                names.add(child.id)
            elif isinstance(child, ast.Attribute):
                names.add(child.attr)
    return names


def _names_docker(function: ast.FunctionDef) -> bool:
    return any(
        isinstance(child, ast.Constant) and child.value == "docker" for child in ast.walk(function)
    )


def _reaches_docker(roots: list[ast.AST]) -> bool:
    definitions = _module_functions()
    pending = list(_referenced_names(roots))
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for function in definitions.get(name, []):
            if _names_docker(function):
                return True
            pending.extend(_referenced_names([function]))
    return False


def _command_of(node: ast.If) -> str | None:
    test = node.test
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.left, ast.Attribute)
        and test.left.attr == "command"
    ):
        return None
    comparator = test.comparators[0]
    if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
        return comparator.value
    return None


def _dispatch() -> tuple[list[ast.stmt], dict[str, list[ast.stmt]]]:
    main = _function("provision_environment", "main")
    branches: dict[str, list[ast.stmt]] = {}
    for node in ast.walk(main):
        if isinstance(node, ast.If):
            command = _command_of(node)
            if command is not None:
                branches[command] = node.body
    shared = [
        statement
        for statement in main.body
        if not (isinstance(statement, ast.If) and _command_of(statement) is not None)
    ]
    return shared, branches


def test_seat_run_subcommands_do_not_reach_docker() -> None:
    shared, branches = _dispatch()
    for command in SEAT_RUN_COMMANDS:
        assert command in branches, f"no dispatch branch for {command}"
        assert not _reaches_docker([*shared, *branches[command]]), command


def test_seat_run_functions_do_not_reach_docker() -> None:
    for module, name in SEAT_RUN_FUNCTIONS:
        assert not _reaches_docker([_function(module, name)]), f"{module}.{name}"


def test_the_walk_reaches_docker_from_the_host_lane_write_compose() -> None:
    shared, branches = _dispatch()
    assert _reaches_docker([*shared, *branches[HOST_LANE_COMMAND]])
