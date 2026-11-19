"""Pins for the static runner lifecycle inventory (WP-1; design-v2 sections 4.1, 4.3, 4.6, 5.14).

Source is parsed with ``ast``; the construction-order pin is the only test that imports the
runner, and it patches the phase callables before calling ``GobbyRunner.create``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "gobby"
INVENTORY_PATH = REPO / "tests" / "contracts" / "runner_lifecycle.json"
CENSUS_PATH = REPO / "tests" / "contracts" / "runner_census.json"
DOC_PATH = REPO / "docs" / "contracts" / "runner-lifecycle.md"
RUNNER_FILE = SRC / "runner.py"
LIFECYCLE_FILES = sorted(SRC.glob("runner_lifecycle*.py"))
SCOPED_FILES = [RUNNER_FILE, *LIFECYCLE_FILES]
SCOPED_MODULES = {"gobby.runner", *(f"gobby.{path.stem}" for path in LIFECYCLE_FILES)}
APP_LIFECYCLE = SRC / "servers" / "_app_lifecycle.py"
SUBSYSTEMS = SRC / "runner_lifecycle_subsystems.py"
PERIODIC = SRC / "runner_lifecycle_periodic.py"
SHUTDOWN = SRC / "runner_lifecycle_shutdown.py"
SHUTDOWN_MODULE = "gobby.runner_lifecycle_shutdown"
FIXTURES = ("tests/contracts/runner_lifecycle.json", "tests/contracts/runner_census.json")
FACADE_PATHS = ("src/gobby/worker", "tests/worker")

STAGES = {"preflight", "construct", "serve"}
PHASE_STAGES = {"construct", "subsystems", "recovery"}
NEXT_OWNERS = {"R1", "R2", "R3", "R4", "R5", "R6"}
TOP_KEYS = {
    "inventory_version",
    "base",
    "create_body",
    "phases",
    "tasks",
    "services",
    "identities",
    "errors",
    "outside_placements",
}
TASK_KEYS = {
    "id",
    "creator",
    "spawned_from",
    "target",
    "runner_attr",
    "name",
    "stage",
    "hub_only",
    "cancel_owner",
    "condition",
    "next_owner",
    "cadence",
    "one_shot",
    "census",
    "observed",
    "debt",
    "disabled_in_profile",
    "runtime_attributed",
}
PHASE_KEYS = {"id", "stage", "owner", "hub_only", "next_owner", "seams"}
SERVICE_KEYS = {"id", "start_owner", "stop_owner", "stage", "hub_only", "next_owner"}
IDENTITY_KEYS = {"name", "source", "producer", "consumers", "current_owner"}
ERROR_KEYS = {"raised_by", "exception", "exit_code", "effect"}
IDENTITY_SOURCES = {"env", "pipe", "file", "database", "process"}
EFFECTS = {"refusal exit 78", "crash exit 1", "clean exit 0", "clean return", "propagates"}
CREATE_CALLS = {"create_task", "ensure_future"}
STOP_ATTRS = {"stop", "close", "aclose", "shutdown"}
LINE_CITE = re.compile(r"runner\.py:\d")


def _inventory() -> dict[str, Any]:
    """Load the frozen inventory; absent until C1b lands the fixture."""
    data: dict[str, Any] = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    return data


def _census() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(CENSUS_PATH.read_text(encoding="utf-8"))
    return data


def _census_key(key: dict[str, str]) -> str:
    """The string form of a census key, as the inventory's ``census`` lists store it."""
    return f"{key['kind']}|{key['creator']}|{key['target']}"


def _module_of(path: Path) -> str:
    return ".".join(path.relative_to(SRC.parent).with_suffix("").parts)


def _source_of(module: str) -> str:
    return (SRC.parent / (module.replace(".", "/") + ".py")).read_text(encoding="utf-8")


def _path_of(module: str) -> Path:
    return SRC.parent / (module.replace(".", "/") + ".py")


def _definitions(tree: ast.AST, prefix: str = "", in_function: bool = False) -> dict[str, ast.AST]:
    """Every def and class in a module, keyed by its ``__qualname__`` (nested defs use <locals>)."""
    found: dict[str, ast.AST] = {}
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if in_function:
                qual = f"{prefix}.<locals>.{child.name}"
            else:
                qual = f"{prefix}.{child.name}" if prefix else child.name
            found[qual] = child
            found.update(_definitions(child, qual, not isinstance(child, ast.ClassDef)))
        else:
            found.update(_definitions(child, prefix, in_function))
    return found


def _function_node(owner: Mapping[str, str]) -> ast.AST | None:
    """The def an owner names, or None when the module defines no such qualname."""
    return _definitions(_parse(_path_of(owner["module"]))).get(owner["qualname"])


def _string_constants(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def _owner_text(owner: Mapping[str, str] | str | None) -> str | None:
    if owner is None or isinstance(owner, str):
        return owner
    return f"{owner['module']}:{owner['qualname']}"


def _walk_scoped(tree: ast.AST) -> Iterator[tuple[ast.AST, str]]:
    """Yield every node with the ``__qualname__`` of its innermost enclosing scope."""

    def visit(node: ast.AST, scope: str, in_function: bool) -> Iterator[tuple[ast.AST, str]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                if in_function:
                    qual = f"{scope}.<locals>.{child.name}"
                elif scope == "<module>":
                    qual = child.name
                else:
                    qual = f"{scope}.{child.name}"
                yield child, scope
                yield from visit(child, qual, not isinstance(child, ast.ClassDef))
            else:
                yield child, scope
                yield from visit(child, scope, in_function)

    yield from visit(tree, "<module>", False)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _call_leaf(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _in_scope(module: str) -> bool:
    return module in SCOPED_MODULES


def _pair(value: dict[str, str] | None) -> tuple[str, str] | None:
    if value is None:
        return None
    return (value["module"], value["qualname"])


def _is_create_call(node: ast.AST) -> bool:
    """True for a create_task, ensure_future or threading.Thread call."""
    if not isinstance(node, ast.Call):
        return False
    leaf = _call_leaf(node.func)
    is_thread = leaf == "Thread" and (
        isinstance(node.func, ast.Name)
        or (isinstance(node.func, ast.Attribute) and ast.unparse(node.func.value) == "threading")
    )
    return leaf in CREATE_CALLS or is_thread


def _create_sites() -> set[tuple[str, str]]:
    """(module, qualname) of every create_task, ensure_future or threading.Thread call."""
    sites: set[tuple[str, str]] = set()
    for path in SCOPED_FILES:
        for node, scope in _walk_scoped(_parse(path)):
            if _is_create_call(node):
                sites.add((_module_of(path), scope))
    return sites


def _spawn_call_count() -> int:
    """Number of periodic spawn calls with a literal name: each one starts one task."""
    count = 0
    for node, _scope in _walk_scoped(_parse(PERIODIC)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "spawn"
        ):
            if node.args and isinstance(node.args[0], ast.Constant):
                count += 1
    return count


def _create_call_counts() -> Counter[tuple[str, str]]:
    """Expected tasks per creator: one per create call, or one per spawn call for the spawn helper."""
    spawn_calls = _spawn_call_count()
    counts: Counter[tuple[str, str]] = Counter()
    for path in SCOPED_FILES:
        for node, scope in _walk_scoped(_parse(path)):
            if _is_create_call(node):
                per_call = spawn_calls if scope.endswith(".<locals>.spawn") else 1
                counts[(_module_of(path), scope)] += per_call
    return counts


def _spawn_sites() -> set[tuple[str, str]]:
    """(module, qualname) of every periodic ``spawn(name, start)`` call."""
    sites: set[tuple[str, str]] = set()
    for path in SCOPED_FILES:
        for node, scope in _walk_scoped(_parse(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "spawn"
            ):
                sites.add((_module_of(path), scope))
    return sites


def _literal_phase_ids(path: Path, function: str) -> list[str]:
    """String first arguments of timed_startup_phase and hub_only_phase calls in one function."""
    found: list[tuple[int, int, str]] = []
    for node, scope in _walk_scoped(_parse(path)):
        if not isinstance(node, ast.Call):
            continue
        if scope != function and not scope.startswith(function + "."):
            continue
        if _call_leaf(node.func) not in {"timed_startup_phase", "hub_only_phase"}:
            continue
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.append((node.lineno, node.col_offset, first.value))
    return [name for _, _, name in sorted(found)]


def _hub_only_constant() -> set[str]:
    for node in _parse(SUBSYSTEMS).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "HUB_ONLY_STARTUP_PHASES"
            for target in node.targets
        ):
            return {
                str(element.value)
                for element in ast.walk(node.value)
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
    raise AssertionError("HUB_ONLY_STARTUP_PHASES literal not found")


def _create_body_digest() -> str:
    """sha256 of the normalized AST of GobbyRunner.create's body (design section 5.8)."""

    class _Normalize(ast.NodeTransformer):
        def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST | None:
            if (node.module or "").startswith("gobby.worker."):
                return None
            return node

        def visit_AsyncWith(self, node: ast.AsyncWith) -> ast.AST | list[ast.stmt]:
            self.generic_visit(node)
            context = node.items[0].context_expr
            if (
                len(node.items) == 1
                and isinstance(context, ast.Call)
                and _call_leaf(context.func) == "phase_scope"
            ):
                return node.body
            return node

    create: ast.AsyncFunctionDef | ast.FunctionDef | None = None
    for node in _parse(RUNNER_FILE).body:
        if isinstance(node, ast.ClassDef) and node.name == "GobbyRunner":
            for item in node.body:
                if (
                    isinstance(item, ast.AsyncFunctionDef | ast.FunctionDef)
                    and item.name == "create"
                ):
                    create = item
    assert create is not None, "GobbyRunner.create not found"
    body = list(create.body)
    first = body[0] if body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        if isinstance(first.value.value, str):
            body = body[1:]
    normalized: list[str] = []
    for statement in body:
        result = _Normalize().visit(statement)
        for item in result if isinstance(result, list) else [result]:
            if isinstance(item, ast.AST):
                normalized.append(ast.dump(item, annotate_fields=True, include_attributes=False))
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True
    )
    return completed.stdout


# 1.1.1 -------------------------------------------------------------------------------------------


def test_inventory_schema_fields() -> None:
    inventory = _inventory()
    assert set(inventory) == TOP_KEYS
    assert inventory["inventory_version"] == 1
    for task in inventory["tasks"]:
        assert set(task) == TASK_KEYS, task.get("id")
        assert task["stage"] in STAGES, task["id"]
        assert task["next_owner"] in NEXT_OWNERS, task["id"]
        assert isinstance(task["hub_only"], bool), task["id"]
        assert task["creator"] is not None
        assert {"module", "qualname"} <= set(task["creator"]), task["id"]
    for phase in inventory["phases"]:
        assert set(phase) == PHASE_KEYS, phase.get("id")
        assert phase["stage"] in PHASE_STAGES, phase["id"]
        assert phase["next_owner"] in NEXT_OWNERS, phase["id"]
        assert phase["owner"] is not None, phase["id"]
    for service in inventory["services"]:
        assert set(service) == SERVICE_KEYS, service.get("id")
        assert service["start_owner"], service["id"]
        assert service["stop_owner"] is not None, service["id"]
        assert service["next_owner"] in NEXT_OWNERS, service["id"]
    for identity in inventory["identities"]:
        assert set(identity) == IDENTITY_KEYS, identity.get("name")
        assert identity["source"] in IDENTITY_SOURCES, identity["name"]
        assert identity["current_owner"] in {"rust", "python"}, identity["name"]
    for error in inventory["errors"]:
        assert set(error) == ERROR_KEYS, error.get("raised_by")
        assert (error["exception"] is None) != (error["exit_code"] is None), error["raised_by"]
        assert error["effect"] in EFFECTS, error["raised_by"]


# 1.1.2 -------------------------------------------------------------------------------------------


def test_runner_lifecycle_doc_states_schema() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    for term in ("inventory_version", "same commit", "plan, not a completion claim"):
        assert term in text, term
    for stage in ("preflight", "construct", "serve", "subsystems", "recovery", "shutdown"):
        assert f"`{stage}`" in text, stage
    for owner in sorted(NEXT_OWNERS):
        assert f"`{owner}`" in text, owner


# 1.1.3 -------------------------------------------------------------------------------------------


def _recorded_construct_order() -> list[str]:
    """Patch the runner_init phase callables, run GobbyRunner.create, return the call order.

    Follows tests/runner_init/test_config_runtime_startup.py::_patch_runner_phases: the storage
    phase installs a stand-in ConfigRuntime, and the runtime-services phase is replaced as a method.
    Nothing here opens a database or a socket.
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import patch

    from gobby.runner import GobbyRunner

    events: list[str] = []

    class _ConfigRuntime:
        async def start(self) -> Any:
            events.append("config_runtime")
            return SimpleNamespace(active=SimpleNamespace())

        async def close(self) -> None:
            return None

    def storage(
        runner: Any, _path: object, _verbose: bool, *, broadcast_loop: object = None
    ) -> None:
        events.append("storage_and_config")
        runner.config_runtime = _ConfigRuntime()
        runner.bootstrap_config = SimpleNamespace()

    def refusal(_runner: Any) -> None:
        events.append("content_refusal")

    def content(_runner: Any) -> None:
        events.append("startup_content")

    def overlay(_active: object, _bootstrap: object) -> Any:
        return SimpleNamespace(agent_build=SimpleNamespace(build_root=None))

    def build_root(_root: object) -> None:
        return None

    async def services(_runner: Any) -> None:
        events.append("runtime_services")

    with (
        patch("gobby.runner_init.open_storage_and_config", storage),
        patch("gobby.runner_init.bundled_content_refusal", refusal),
        patch("gobby.runner_init.init_startup_content", content),
        patch("gobby.runner_init.storage.bootstrap_overlaid_config", overlay),
        patch("gobby.agents.build_root.configure_build_root", build_root),
        patch.object(GobbyRunner, "_initialize_runtime_services", services),
    ):
        asyncio.run(GobbyRunner.create())
    return events


def test_startup_phases_match_runner_order() -> None:
    phases = _inventory()["phases"]
    for stage, function in (
        ("subsystems", "init_subsystems"),
        ("recovery", "_recover_after_restart"),
    ):
        expected = [phase["id"] for phase in phases if phase["stage"] == stage]
        assert _literal_phase_ids(SUBSYSTEMS, function) == expected, stage
    hub_only_ids = {phase["id"] for phase in phases if phase["hub_only"]}
    assert _hub_only_constant() == hub_only_ids
    construct = [phase["id"] for phase in phases if phase["stage"] == "construct"]
    assert _recorded_construct_order() == construct


# 1.1.4 -------------------------------------------------------------------------------------------


def test_task_creation_sites_are_inventoried() -> None:
    tasks = _inventory()["tasks"]
    # A runtime-attributed creator (R29) is the frame a construct task reports, not a call
    # site in the scoped modules, so the static site pins skip it.
    in_scope = [
        task
        for task in tasks
        if _in_scope(task["creator"]["module"]) and not task["runtime_attributed"]
    ]
    creators = {_pair(task["creator"]) for task in in_scope}
    assert creators == _create_sites()
    counted: Counter[tuple[str, str] | None] = Counter(_pair(task["creator"]) for task in in_scope)
    assert counted == _create_call_counts(), (counted, _create_call_counts())
    spawned = {_pair(task["spawned_from"]) for task in tasks if task["spawned_from"] is not None}
    assert spawned == _spawn_sites()


# 1.1.5 -------------------------------------------------------------------------------------------


def _periodic_names() -> tuple[set[str], set[str]]:
    names: set[str] = set()
    for node, _scope in _walk_scoped(_parse(PERIODIC)):
        if not isinstance(node, ast.Call):
            continue
        first = node.args[0] if node.args else None
        if isinstance(node.func, ast.Name) and node.func.id == "spawn":
            if isinstance(first, ast.Constant):
                names.add(str(first.value))
        if _call_leaf(node.func) in CREATE_CALLS:
            for keyword in node.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    names.add(str(keyword.value.value))
    hub_only: set[str] = set()
    for node in _parse(PERIODIC).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "HUB_ONLY_PERIODIC_TASKS"
            for target in node.targets
        ):
            hub_only = {
                str(element.value)
                for element in ast.walk(node.value)
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
    return names, hub_only


def _cancel_tuple_attrs() -> set[str]:
    for node, _scope in _walk_scoped(_parse(SHUTDOWN)):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "periodic_task_attrs"
            for target in node.targets
        ):
            return {
                str(element.value)
                for element in ast.walk(node.value)
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
    raise AssertionError("periodic_task_attrs literal not found in runner_lifecycle_shutdown.py")


def test_periodic_tasks_match_spawn_and_cancel_owners() -> None:
    tasks = _inventory()["tasks"]
    names, hub_only = _periodic_names()
    assert len(names) == 24
    periodic = [task for task in tasks if task["id"] in names]
    assert {task["id"] for task in periodic} == names
    assert {task["id"] for task in periodic if task["hub_only"]} == hub_only
    cancel_attrs = _cancel_tuple_attrs()
    for task in periodic:
        attr = task["runner_attr"]
        assert attr is not None, task["id"]
        # Every periodic attribute is cancelled by name in the shutdown module. The owner must
        # be a shutdown function whose body names the attribute: _cancel_periodic_tasks for the
        # tuple, and stop_restart_recovery for _hook_inbox_task, which it cancels first.
        assert attr in cancel_attrs, task["id"]
        cancel = task["cancel_owner"]
        assert cancel is not None and cancel["module"] == SHUTDOWN_MODULE, task["id"]
        assert attr in _string_constants(_function_node(cancel)), (task["id"], cancel)


# 1.1.6 -------------------------------------------------------------------------------------------


def _resolve_receiver(expr: ast.expr, bindings: dict[str, str]) -> str | None:
    """Resolve a receiver to a runner attribute path, one hop through local bindings."""
    if isinstance(expr, ast.Name):
        if expr.id in {"runner", "self"}:
            return None
        return bindings.get(expr.id)
    if isinstance(expr, ast.Attribute):
        if isinstance(expr.value, ast.Name) and expr.value.id in {"runner", "self"}:
            return expr.attr
        inner = _resolve_receiver(expr.value, bindings)
        return f"{inner}.{expr.attr}" if inner else None
    if isinstance(expr, ast.Call) and _call_leaf(expr.func) == "getattr" and len(expr.args) >= 2:
        target = expr.args[1]
        if not (isinstance(target, ast.Constant) and isinstance(target.value, str)):
            return None
        base = expr.args[0]
        if isinstance(base, ast.Name) and base.id in {"runner", "self"}:
            return target.value
        inner = _resolve_receiver(base, bindings)
        return f"{inner}.{target.value}" if inner else None
    return None


def _function_bindings(function: ast.AST) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                resolved = _resolve_receiver(node.value, {})
                if resolved is not None:
                    bindings[target.id] = resolved
    return bindings


def _start_receivers() -> set[str]:
    """Attribute paths of every start call and _start_tracked_service first argument in scope."""
    found: set[str] = set()
    for path in SCOPED_FILES:
        for function in ast.walk(_parse(path)):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            bindings = _function_bindings(function)
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute) and node.func.attr == "start":
                    if isinstance(node.func.value, ast.Name) and node.func.value.id == "service":
                        continue  # the generic start inside _start_tracked_service
                    resolved = _resolve_receiver(node.func.value, bindings)
                    if resolved is not None:
                        found.add(resolved)
                if _call_leaf(node.func) == "_start_tracked_service" and node.args:
                    resolved = _resolve_receiver(node.args[0], bindings)
                    if resolved is not None:
                        found.add(resolved)
    return found


def _stop_receivers() -> set[str]:
    """Attribute paths whose stop, close, aclose or shutdown is reached in the shutdown module."""
    found: set[str] = set()
    for function in ast.walk(_parse(SHUTDOWN)):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        bindings = _function_bindings(function)
        for node in ast.walk(function):
            if isinstance(node, ast.Attribute) and node.attr in STOP_ATTRS:
                resolved = _resolve_receiver(node.value, bindings)
                if resolved is not None:
                    found.add(resolved)
    return found


def test_started_and_stopped_services_are_inventoried() -> None:
    services = {service["id"]: service for service in _inventory()["services"]}
    starts = _start_receivers()
    assert starts <= set(services), sorted(starts - set(services))
    stops = _stop_receivers()
    assert stops <= set(services), sorted(stops - set(services))
    for service_id, service in services.items():
        attr = service_id.split(".")[-1]
        for owner in service["start_owner"]:
            assert attr in _source_of(owner["module"]), (service_id, owner)
        for owner in service["stop_owner"]:
            assert attr in _source_of(owner["module"]), (service_id, owner)


# 1.1.7 -------------------------------------------------------------------------------------------


def _raise_and_exit_entries() -> set[tuple[str, str, str]]:
    """(module, qualname, kind:name) for every explicit raise and sys.exit(<int>) in scope."""
    entries: set[tuple[str, str, str]] = set()
    for path in SCOPED_FILES:
        module = _module_of(path)
        for node, scope in _walk_scoped(_parse(path)):
            if isinstance(node, ast.Raise) and node.exc is not None:
                exc = node.exc
                if isinstance(exc, ast.Call):
                    name = _call_leaf(exc.func) or ast.unparse(exc)
                else:
                    name = ast.unparse(exc)
                entries.add((module, scope, f"exception:{name}"))
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "sys.exit":
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant):
                    entries.add((module, scope, f"exit_code:{first.value!r}"))
    return entries


# Every runner module, the modules that consume the identities. The identity producers are
# imported here, so each producer's identity is spelled in at least one of these files.
IDENTITY_FILES = tuple(sorted(SRC.glob("runner*.py")))
ENV_IDENTITY = re.compile(r"^GOBBY_[A-Z0-9_]+$")
APPLICATION_NAME = re.compile(r"\bapplication_name\b")
# A public credential or lease name. A private helper (``_read_credential``) is not a spelling.
IDENTITY_SPELLING = re.compile(
    r"^[A-Za-z]\w*(?:_token|_credential|_machine_id)$|^[A-Za-z]\w*LeaseView$"
)


def _spelled_names(node: ast.AST) -> list[str]:
    """Names a node spells as a Name, an import alias, an attribute or a def or class."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.alias):
        return [node.name]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [node.name]
    return []


def _source_identity_names() -> set[str]:
    """Identity names the runner modules spell (R27), derived from the source alone.

    GOBBY_* environment names and the application_name the database identity uses are matched
    as strings. Credential, token, machine-id and lease-view names are matched as spelled names.
    The producer modules sit outside this scan on purpose: a producer's name reaches the runner
    through an import, and a producer's own helpers are not identities.
    """
    names: set[str] = set()
    for path in IDENTITY_FILES:
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if ENV_IDENTITY.match(node.value):
                    names.add(node.value)
                if APPLICATION_NAME.search(node.value):
                    names.add("application_name")
                continue
            names.update(name for name in _spelled_names(node) if IDENTITY_SPELLING.match(name))
    return names


def test_source_identities_are_inventoried() -> None:
    """Deleting any source-spelled identity from the inventory fails this pin, and so does an
    inventory identity that no runner module spells (R27)."""
    inventory_names = {identity["name"] for identity in _inventory()["identities"]}
    spelled = _source_identity_names()
    assert not spelled - inventory_names, sorted(spelled - inventory_names)
    assert not inventory_names - spelled, sorted(inventory_names - spelled)


def test_identities_and_errors_are_inventoried() -> None:
    inventory = _inventory()
    actual: set[tuple[str, str, str]] = set()
    for error in inventory["errors"]:
        owner = error["raised_by"]
        if error["exception"] is not None:
            kind = f"exception:{error['exception']}"
        else:
            kind = f"exit_code:{error['exit_code']}"
        actual.add((owner["module"], owner["qualname"], kind))
    assert actual == _raise_and_exit_entries()
    for identity in inventory["identities"]:
        producer = identity["producer"]
        if producer["module"].endswith(".rs"):
            # A Rust producer names a crate file by its repository path and a Rust qualname.
            rust_source = (REPO / producer["module"]).read_text(encoding="utf-8")
            assert producer["qualname"].split("::")[-1] in rust_source, identity["name"]
        else:
            assert producer["qualname"].split(".")[-1] in _source_of(producer["module"]), identity[
                "name"
            ]
        for consumer in identity["consumers"]:
            assert identity["name"] in _source_of(consumer["module"]), (identity["name"], consumer)


# O1, O3 and C1b pins ----------------------------------------------------------------------------


def test_fixture_commits_precede_facade() -> None:
    """Both fixtures are first added by commits that precede the first facade-path commit (O1).

    Passes only after the port session commits C1b and C1c; until then the fixture has no
    adding commit and the first assertion names it.
    """
    order = _git("rev-list", "--reverse", "HEAD").split()
    position = {sha: index for index, sha in enumerate(order)}
    fixture_positions: list[int] = []
    for fixture in FIXTURES:
        added = _git("log", "--reverse", "--diff-filter=A", "--format=%H", "--", fixture).split()
        assert added, f"{fixture} is not yet added by any commit"
        fixture_positions.append(position[added[0]])
    facade = _git("log", "--reverse", "--diff-filter=A", "--format=%H", "--", *FACADE_PATHS).split()
    if facade:
        assert position[facade[0]] > max(fixture_positions), "facade path added before fixtures"


def _owner_from_text(text: str) -> dict[str, str]:
    """A phase owner written as path::qualname (the form the phase parsers read)."""
    path, qualname = text.split("::", 1)
    module = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    return {"module": module, "qualname": qualname}


def _owner_refs(entry: dict[str, Any]) -> Iterator[dict[str, str]]:
    for key in ("creator", "spawned_from", "target", "cancel_owner", "owner", "raised_by"):
        value = entry.get(key)
        if isinstance(value, dict) and "module" in value:
            yield value
        if isinstance(value, str) and "::" in value:
            yield _owner_from_text(value)
    for key in ("start_owner", "stop_owner", "consumers"):
        for value in entry.get(key) or []:
            if isinstance(value, dict) and "module" in value:
                yield value
    producer = entry.get("producer")
    if isinstance(producer, dict) and "module" in producer:
        yield producer


def test_app_lifecycle_absent_from_inventory() -> None:
    inventory = _inventory()
    for section in ("phases", "tasks", "services", "identities", "errors"):
        for entry in inventory[section]:
            for ref in _owner_refs(entry):
                assert ref["module"] != "gobby.servers._app_lifecycle", (section, entry)


def _is_hook_manager_construction(node: ast.Call) -> bool:
    """The HookManager is built by calling the factory the getter returns (R21). Its
    constructor starts the workflow evaluation thread."""
    return isinstance(node.func, ast.Call) and _call_leaf(node.func.func) == (
        "hook_manager_factory_getter"
    )


def test_app_lifecycle_starts_are_censused() -> None:
    """Every task, thread or monitor start in _app_lifecycle.py is censused (R18, R21, R22).

    The census cites are the observed ones plus the static ones, so the source sites must
    equal the union of both.
    """
    cites = {entry["cite"] for entry in _census()["outside_runner_lifecycle"] if entry["cite"]}
    sites: set[str] = set()
    for node, _scope in _walk_scoped(_parse(APP_LIFECYCLE)):
        if not isinstance(node, ast.Call):
            continue
        leaf = _call_leaf(node.func)
        if leaf in {
            "start",
            "start_vision_temp_cleanup_task",
            *CREATE_CALLS,
        } or _is_hook_manager_construction(node):
            sites.add(f"src/gobby/servers/_app_lifecycle.py:{node.lineno}")
    assert sites == cites


def test_outside_runner_lifecycle_services_absent_from_inventory() -> None:
    inventory = _inventory()
    for service in inventory["services"]:
        for owner in [*service["start_owner"], *service["stop_owner"]]:
            assert owner["module"] in SCOPED_MODULES, (service["id"], owner)
    for task in inventory["tasks"]:
        # Code-index loops are recurring entries from outside the scoped modules; the entries
        # mark them disabled-in-profile (R28), so the absence check allows them.
        if task["stage"] in {"serve", "preflight"} and not task["disabled_in_profile"]:
            assert task["creator"]["module"] in SCOPED_MODULES, task["id"]
    census = _census()
    mapped = {key for task in inventory["tasks"] for key in task["census"]}
    outside = {entry["key"] for entry in census["outside_runner_lifecycle"]}
    # No creator scope exempts a census task: an in-scope key deleted from its inventory list
    # has no other owner and must fail here.
    for record in census["tasks"]:
        key = _census_key(record["key"])
        assert key in mapped or key in outside, key


def test_create_body_matches_base_digest() -> None:
    inventory = _inventory()
    assert _create_body_digest() == inventory["create_body"]["sha256"]
    _git("cat-file", "-e", f"{inventory['base']['commit']}^{{commit}}")


def test_cites_into_runner_py_have_no_line_numbers() -> None:
    for text in _strings(_inventory()):
        assert not LINE_CITE.search(text), text


# 1.2.3 -------------------------------------------------------------------------------------------


def test_constructor_tasks_are_inventoried() -> None:
    by_key = {key: task for task in _inventory()["tasks"] for key in task["census"]}
    for record in _census()["tasks"]:
        if record["stage"] != "construct":
            continue
        key = _census_key(record["key"])
        assert key in by_key, key
        task = by_key[key]
        assert task["stage"] == "construct", key
        assert "cancel_owner" in task, key
        assert task["next_owner"] in NEXT_OWNERS, key


# 1.2.4 -------------------------------------------------------------------------------------------


def test_runner_lifecycle_doc_census_section() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    heading = re.search(r"^## Runtime census\s*$", text, re.MULTILINE)
    assert heading is not None, "the doc has no '## Runtime census' heading"
    section = text[heading.end() :]
    for term in ("record", "key", "skip"):
        assert term in section, term


# O2, inventory side -----------------------------------------------------------------------------


def test_every_recurring_task_has_creator_target_cadence() -> None:
    periodic, _hub_only = _periodic_names()
    for task in _inventory()["tasks"]:
        if task["id"] in periodic:
            assert task["cadence"] is not None, task["id"]
        if task["cadence"] is None:
            continue
        assert task["creator"] is not None, task["id"]
        assert task["target"] is not None, task["id"]
        cadence = task["cadence"]
        assert {"seconds", "param", "default", "config_key", "wake", "source"} <= set(cadence)
        assert cadence["source"], task["id"]
        assert cadence["seconds"] is not None or (
            cadence["param"] is not None and cadence["default"] is not None
        ), task["id"]


EXECUTOR_KEY = re.compile(r"^executor\|[^|]+\|[^|]*$")


def _census_keys(census: Mapping[str, Any]) -> tuple[set[str], set[str], set[str]]:
    """Task, thread and executor key strings the census observed."""
    tasks = {_census_key(record["key"]) for record in census["tasks"]}
    threads = {_census_key(record["key"]) for record in census["threads"]}
    executors = {f"executor|{row['creator']}|{row['prefix']}" for row in census["executors"]}
    return tasks, threads, executors


def test_executor_keys_carry_no_worker_count() -> None:
    """An executor key is executor|creator|prefix. A worker count in it would let a host's CPU
    count flip an owner (finding 1), so no key may end in a count."""
    inventory = _inventory()
    keys = {key for task in inventory["tasks"] for key in task["census"] if "executor|" in key}
    keys |= {item["key"] for item in inventory["outside_placements"] if "executor|" in item["key"]}
    assert keys
    for key in sorted(keys):
        assert EXECUTOR_KEY.match(key), key
        assert not re.search(r"\|\d+$", key), key


def test_census_keys_reconcile_with_inventory() -> None:
    """Every observed key has exactly one owner, and every owner has an observed key (R23).

    A census task, thread or executor key is listed by one inventory task or placed outside.
    An inventory task that observed its keys lists only observed keys. The outside placements
    are the census outside keys plus the executors that no inventory task owns.
    """
    inventory = _inventory()
    census = _census()
    listed: Counter[str] = Counter(key for task in inventory["tasks"] for key in task["census"])
    assert not [key for key, count in listed.items() if count > 1], "a key has two owners"
    tasks, threads, executors = _census_keys(census)
    observed = tasks | threads | executors
    placements = {item["key"] for item in inventory["outside_placements"]}
    outside = {entry["key"] for entry in census["outside_runner_lifecycle"] if entry["key"]}
    assert not observed - set(listed) - placements, sorted(observed - set(listed) - placements)
    for task in inventory["tasks"]:
        if task["observed"]:
            assert task["census"], task["id"]
            assert set(task["census"]) <= observed, (
                task["id"],
                sorted(set(task["census"]) - observed),
            )
        else:
            assert not task["census"], task["id"]
    assert not (outside & observed), sorted(outside & observed)
    assert placements == outside | (executors - set(listed)), sorted(
        placements ^ (outside | (executors - set(listed)))
    )


def test_unobserved_matches_recurring_inventory() -> None:
    """census.unobserved lists exactly the recurring inventory entries no boot observed (R22, R28).

    One-shot tasks have no cadence and are not expected in the census at all.
    """
    inventory = _inventory()
    recurring_unobserved = {
        task["id"]
        for task in inventory["tasks"]
        if task["cadence"] is not None and not task["observed"]
    }
    assert {row["id"] for row in _census()["unobserved"]} == recurring_unobserved


def test_cancel_owner_present_iff_debt_tag() -> None:
    """A task has a verified cancel owner, or it carries the R0 debt tag and no owner (R12)."""
    for task in _inventory()["tasks"]:
        assert task["debt"] in (None, "debt:R0"), task["id"]
        assert (task["cancel_owner"] is None) == (task["debt"] == "debt:R0"), task["id"]


def test_cancel_and_stop_owners_resolve_to_definitions() -> None:
    """Every gobby owner the inventory names is a def or class in its module (finding 7a).

    Owners outside gobby (asyncio, anyio, starlette) are library calls and are not resolved.
    """
    inventory = _inventory()
    owners: set[str] = set()
    for task in inventory["tasks"]:
        if task["cancel_owner"] is not None:
            owners.add(_owner_text(task["cancel_owner"]) or "")
    for item in inventory["outside_placements"]:
        if item["stop_owner"] is not None:
            owners.add(item["stop_owner"])
    for service in inventory["services"]:
        owners.update(
            _owner_text(owner) or "" for owner in [*service["start_owner"], *service["stop_owner"]]
        )
    for text in sorted(owners):
        module, qualname = text.split(":", 1)
        if not module.startswith("gobby."):
            continue
        assert qualname in _definitions(_parse(_path_of(module))), text


def test_outside_placements_have_owner_or_debt() -> None:
    """Each outside placement names a stop owner, or carries the R0 debt tag and no owner (R12)."""
    for item in _inventory()["outside_placements"]:
        assert item["debt"] in (None, "debt:R0"), item["key"]
        assert (item["stop_owner"] is None) == (item["debt"] == "debt:R0"), item["key"]


def test_census_owners_match_inventory() -> None:
    """A census row's owner is the owner its inventory task or outside placement names (R26)."""
    inventory = _inventory()
    census = _census()
    mapped = {key: task for task in inventory["tasks"] for key in task["census"]}
    placed = {item["key"]: item for item in inventory["outside_placements"]}
    for section in ("tasks", "threads", "executors"):
        for row in census[section]:
            text = (
                _census_key(row["key"])
                if section != "executors"
                else f"executor|{row['creator']}|{row['prefix']}"
            )
            if text in mapped:
                expected = _owner_text(mapped[text]["cancel_owner"])
            else:
                expected = placed[text]["stop_owner"]
            owner = row.get("cancel_owner")
            assert owner == expected, (section, text, owner, expected)
    for entry in census["outside_runner_lifecycle"]:
        if entry["key"] is None:
            continue
        assert entry["stop_owner"] == placed[entry["key"]]["stop_owner"], entry["key"]
        assert entry["debt"] == placed[entry["key"]]["debt"], entry["key"]


PHASE_CALLS = {"timed_startup_phase", "hub_only_phase"}


def _literal_phase_calls(function: str) -> dict[str, set[str]]:
    """Phase id -> the names its literal phase call reaches, for one function in the subsystems
    module. Every call leaf inside the phase call is a reached name, except the phase wrappers."""
    body = _definitions(_parse(SUBSYSTEMS))[function]
    reached: dict[str, set[str]] = {}
    for call in ast.walk(body):
        if not isinstance(call, ast.Call) or _call_leaf(call.func) not in PHASE_CALLS:
            continue
        first = call.args[0] if call.args else None
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        leaves = {_call_leaf(inner.func) for inner in ast.walk(call) if isinstance(inner, ast.Call)}
        reached[first.value] = {leaf for leaf in leaves if leaf and leaf not in PHASE_CALLS}
    return reached


def test_subsystem_and_recovery_seams_match_their_calls() -> None:
    """A subsystem or recovery phase lists every helper its literal call reaches (R29).

    A seam that is listed but not called, or a called helper that is not listed, fails.
    """
    phases = _inventory()["phases"]
    for stage, function in (
        ("subsystems", "init_subsystems"),
        ("recovery", "_recover_after_restart"),
    ):
        reached = _literal_phase_calls(function)
        for phase in phases:
            if phase["stage"] == stage:
                assert reached.get(phase["id"]) == set(phase["seams"]), phase["id"]
