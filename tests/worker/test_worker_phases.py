"""WP-4 tests (R2c): the phase gate, the driver over the real GobbyRunner.create, and the
dormancy, import-scope and rule-16 checks.

Written against design-v2 sections 4.3, 4.4, 4.6, 5.7, 5.8 and 5.18 and rulings R5 and R6.
The module fails on import until gobby.worker.phase_ids, gobby.worker.gate and
gobby.worker.phases exist (G1 and G2).
"""

from __future__ import annotations

import ast
import functools
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from gobby.worker.gate import phase_scope
from gobby.worker.phases import UNUSABLE, PhaseDriver

from gobby.app_context import clear_app_context
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.runner import GobbyRunner
from gobby.worker.phase_ids import InitPhase

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "tests/contracts/runner_lifecycle.json"
CENSUS = ROOT / "tests/contracts/runner_census.json"
BUNDLED_WORKFLOWS = ROOT / "src/gobby/install/shared/workflows"
BUNDLED_KINDS = ("agents", "pipelines", "rules")
# Every .py and .rs file that R0 owns or adds to (design section 2). The rule-16 scan reads
# only the lines R0 adds under these paths.
R0_SCAN_PATHS = (
    "src/gobby/runner.py",
    "src/gobby/worker",
    "tests/worker",
    "tests/contracts",
    "tests/e2e/task_census_recorder.py",
    "tests/e2e/task_census_bootstrap.py",
    "tests/e2e/test_runner_task_census.py",
    "crates/gdaemon/src/lifecycle",
)


@pytest.fixture(autouse=True)
def _clear_app_context() -> Iterator[None]:
    clear_app_context()
    yield
    clear_app_context()


class _FakeRuntime:
    """Stands in for ConfigRuntime and records the probe value at close."""

    def __init__(self, seams: _Seams) -> None:
        self._seams = seams

    async def start(self) -> SimpleNamespace:
        self._seams.events.append("runtime.start")
        return SimpleNamespace(active=DaemonConfig(ui={"enabled": False}))

    async def close(self) -> None:
        self._seams.events.append("runtime.close")
        self._seams.unusable_at_close.append(self._seams.unusable())


@dataclass
class _Seams:
    """What the patched phase seams record during one real GobbyRunner.create."""

    refusal_message: str | None = None
    events: list[str] = field(default_factory=list)
    storage_args: list[tuple[object, bool]] = field(default_factory=list)
    unusable_at_close: list[bool] = field(default_factory=list)
    unusable_at_rollback: list[bool] = field(default_factory=list)
    probe: Callable[[], bool] | None = None

    def unusable(self) -> bool:
        assert self.probe is not None, "the test must set probe before running create"
        return self.probe()


def _patch_phase_seams(monkeypatch: pytest.MonkeyPatch, seams: _Seams) -> None:
    """Replace only the phase seams and the rollback; GobbyRunner.create runs for real."""

    def storage(
        runner: Any,
        config_path: object,
        verbose: bool,
        *,
        broadcast_loop: object = None,
    ) -> None:
        seams.events.append("storage")
        seams.storage_args.append((config_path, verbose))
        runner.bootstrap_config = BootstrapConfig()
        runner.config_runtime = _FakeRuntime(seams)

    def refusal(_runner: Any) -> str | None:
        seams.events.append("refusal")
        return seams.refusal_message

    def content(_runner: Any) -> None:
        seams.events.append("content")

    def build_root(_value: object) -> None:
        seams.events.append("build_root")

    async def services(_runner: Any) -> None:
        seams.events.append("services")

    async def rollback(_runner: Any) -> None:
        seams.events.append("rollback")
        seams.unusable_at_rollback.append(seams.unusable())

    monkeypatch.setattr("gobby.runner_init.open_storage_and_config", storage)
    monkeypatch.setattr("gobby.runner_init.bundled_content_refusal", refusal)
    monkeypatch.setattr("gobby.runner_init.init_startup_content", content)
    monkeypatch.setattr("gobby.agents.build_root.configure_build_root", build_root)
    monkeypatch.setattr(GobbyRunner, "_initialize_runtime_services", services)
    monkeypatch.setattr("gobby.runner_rollback.rollback_runner_resources_async", rollback)


async def test_gate_is_noop_without_facade() -> None:
    entered: list[InitPhase] = []
    for phase in InitPhase:
        async with phase_scope(phase, object()):
            entered.append(phase)
    assert entered == list(InitPhase)

    failure = ValueError("phase body failed")
    with pytest.raises(ValueError) as caught:
        async with phase_scope(InitPhase.STORAGE_AND_CONFIG, object()):
            raise failure
    assert caught.value is failure


async def test_driver_runs_real_create_in_five_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    seams = _Seams()
    _patch_phase_seams(monkeypatch, seams)
    driver = PhaseDriver(functools.partial(GobbyRunner.create, None, False))
    phases = list(InitPhase)

    # Each initialize runs exactly one phase body, then parks create at the next scope.
    expected_events = [
        ["storage"],
        ["storage", "refusal"],
        ["storage", "refusal", "content"],
        ["storage", "refusal", "content", "runtime.start", "build_root"],
        ["storage", "refusal", "content", "runtime.start", "build_root", "services"],
    ]
    statuses: list[object] = []
    next_phases: list[object] = []
    seen_events: list[list[str]] = []
    for phase in phases:
        outcome = await driver.initialize(phase)
        statuses.append(outcome.status)
        next_phases.append(outcome.next_phase)
        seen_events.append(list(seams.events))

    assert statuses == ["ready"] * 5
    assert next_phases == [*phases[1:], None]
    assert seen_events == expected_events
    assert driver.ready
    assert not driver.unusable
    assert isinstance(driver.owner, GobbyRunner)


async def test_real_refusal_marks_unusable_before_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    seams = _Seams(refusal_message="startup refused by test")
    _patch_phase_seams(monkeypatch, seams)
    driver = PhaseDriver(functools.partial(GobbyRunner.create, None, False))
    seams.probe = lambda: driver.unusable

    first = await driver.initialize(InitPhase.STORAGE_AND_CONFIG)
    assert first.status == "ready"
    refused = await driver.initialize(InitPhase.CONTENT_REFUSAL)

    assert refused.status == "refused"
    assert refused.message == "startup refused by test"
    assert refused.next_phase is None
    assert seams.events == ["storage", "refusal", "runtime.close", "rollback"]
    # R5: the gate marks the driver unusable inside the failing scope, before create's
    # except block runs runtime.close() and the rollback.
    assert seams.unusable_at_close == [True]
    assert seams.unusable_at_rollback == [True]
    assert driver.unusable
    assert await driver.initialize(InitPhase.STARTUP_CONTENT) is UNUSABLE


async def test_create_passes_config_path_and_verbose(monkeypatch: pytest.MonkeyPatch) -> None:
    seams = _Seams()
    _patch_phase_seams(monkeypatch, seams)
    config_path = Path("phase-test-config.yaml")
    # The facade builds the driver from this factory (design GF-20); create itself is unchanged.
    driver = PhaseDriver(functools.partial(GobbyRunner.create, config_path, True))

    for phase in InitPhase:
        assert (await driver.initialize(phase)).status == "ready"
    assert seams.storage_args == [(config_path, True)]


def _load_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), path
    return loaded


def _qualname_of_owner(owner: str) -> str:
    """The qualname of an owner written as path::qualname or module:qualname."""
    return re.split(r"::|:", owner)[-1]


def _qualname_of_creator(creator: str) -> str:
    """The qualname of a census creator written as module:qualname."""
    return creator.split(":", 1)[1]


def test_construct_tasks_match_census() -> None:
    inventory = _load_json(INVENTORY)
    census = _load_json(CENSUS)

    construct_phases = [phase for phase in inventory["phases"] if phase["stage"] == "construct"]
    assert [phase["id"] for phase in construct_phases] == [phase.value for phase in InitPhase]

    # A construct phase lists its owner and the seams it calls. A construct-stage creator
    # belongs to the phase that lists it.
    listed_by: dict[str, set[str]] = {}
    for phase in construct_phases:
        for name in (_qualname_of_owner(phase["owner"]), *phase["seams"]):
            listed_by.setdefault(name, set()).add(phase["id"])

    records = [task for task in census["tasks"] if task["stage"] == "construct"]
    assert records, "the census records no construct-stage task"
    unattributed = [
        (task["creator"], task["target"])
        for task in records
        if _qualname_of_creator(task["creator"]) not in listed_by
    ]
    assert unattributed == []


def _is_worker(module: str) -> bool:
    return module == "gobby.worker" or module.startswith("gobby.worker.")


def _resolve_from(node: ast.ImportFrom, package: str) -> str:
    if node.level == 0:
        return node.module or ""
    parts = package.split(".")
    base = parts[: len(parts) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def _is_dynamic_import(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id == "__import__"
    return isinstance(func, ast.Attribute) and func.attr == "import_module"


def _worker_modules_of(node: ast.AST, package: str) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        module = _resolve_from(node, package)
        found = [module] if _is_worker(module) else []
        if module == "gobby" and any(alias.name == "worker" for alias in node.names):
            found.append("gobby.worker")
        return found
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names if _is_worker(alias.name)]
    if isinstance(node, ast.Call) and _is_dynamic_import(node.func) and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if _is_worker(first.value):
                return [first.value]
    return []


def _worker_imports(tree: ast.Module, package: str) -> Iterator[tuple[str, str]]:
    """Yield (gobby.worker module, enclosing qualname) for every such import in a module."""

    def visit(node: ast.AST, scope: str) -> Iterator[tuple[str, str]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = child.name if scope == "<module>" else f"{scope}.{child.name}"
                yield from visit(child, inner)
                continue
            for module in _worker_modules_of(child, package):
                yield module, scope
            yield from visit(child, scope)

    yield from visit(tree, "<module>")


def _may_import_worker(rel: str) -> bool:
    return rel == "src/gobby/runner.py" or rel.startswith("src/gobby/worker/")


def test_only_runner_imports_worker() -> None:
    src = ROOT / "src"
    hits: list[tuple[str, str, str]] = []
    for path in sorted((src / "gobby").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        package = ".".join(path.relative_to(src).parts[:-1])
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        hits.extend((rel, module, scope) for module, scope in _worker_imports(tree, package))

    # Positive control: the scan must see the lazy imports that create makes.
    assert hits, "the scan found no gobby.worker import, so the scan is broken"
    outside = [hit for hit in hits if not _may_import_worker(hit[0])]
    assert outside == []
    runner_hits = {(module, scope) for rel, module, scope in hits if rel == "src/gobby/runner.py"}
    assert runner_hits == {
        ("gobby.worker.phase_ids", "GobbyRunner.create"),
        ("gobby.worker.gate", "GobbyRunner.create"),
    }


_PROBE = """
import asyncio
import json
import sys
from types import SimpleNamespace

from gobby.runner import GobbyRunner


def worker_modules():
    return sorted(m for m in sys.modules if m == "gobby.worker" or m.startswith("gobby.worker."))


after_import = worker_modules()

import gobby.agents.build_root
import gobby.runner_init
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig


class Runtime:
    async def start(self):
        return SimpleNamespace(active=DaemonConfig(ui={"enabled": False}))

    async def close(self):
        return None


def storage(runner, config_path, verbose, *, broadcast_loop=None):
    runner.bootstrap_config = BootstrapConfig()
    runner.config_runtime = Runtime()


async def services(runner):
    return None


gobby.runner_init.open_storage_and_config = storage
gobby.runner_init.bundled_content_refusal = lambda runner: None
gobby.runner_init.init_startup_content = lambda runner: None
gobby.agents.build_root.configure_build_root = lambda value: None
GobbyRunner._initialize_runtime_services = services

asyncio.run(GobbyRunner.create(None, False))
print(json.dumps({"after_import": after_import, "after_create": worker_modules()}))
"""


def test_worker_modules_not_imported_by_runner() -> None:
    # PYTHONPATH pins the probe to this checkout's src, not an installed copy.
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    # The parent package appears with each submodule import, so it is part of the allowed set.
    assert report["after_import"] == []
    assert report["after_create"] == ["gobby.worker", "gobby.worker.gate", "gobby.worker.phase_ids"]


def _bundled_definition_names() -> set[str]:
    """Names of bundled agent, pipeline and rule definitions (design 4.4).

    A name is the file's top-level name when declared, else its file stem. Only YAML files
    under the three definition directories count. The name default is never collected.
    """
    names: set[str] = set()
    for kind in BUNDLED_KINDS:
        for path in sorted((BUNDLED_WORKFLOWS / kind).rglob("*.yaml")):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            declared = document.get("name") if isinstance(document, dict) else None
            name = declared if isinstance(declared, str) else path.stem
            if name != "default":
                names.add(name)
    return names


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _added_lines(base: str) -> Iterator[tuple[str, str]]:
    """Yield (path, text) for each line R0 adds to a .py or .rs file under R0_SCAN_PATHS.

    Tracked changes come from `git diff --unified=0 <base>`. Untracked new files count in full.
    """
    diff = _git("diff", "--unified=0", base, "--", *R0_SCAN_PATHS)
    current: str | None = None
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = None
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif not in_hunk and line.startswith("+++ "):
            target = line[4:]
            current = target[2:] if target.startswith("b/") else None
        elif in_hunk and line.startswith("+") and current is not None:
            if current.endswith((".py", ".rs")):
                yield current, line[1:]
    untracked = _git("ls-files", "--others", "--exclude-standard", "--", *R0_SCAN_PATHS)
    for rel in untracked.splitlines():
        if rel.endswith((".py", ".rs")):
            for text in (ROOT / rel).read_text(encoding="utf-8").splitlines():
                yield rel, text


def test_no_bundled_definition_names_in_r0_additions() -> None:
    names = _bundled_definition_names()
    assert names, "no bundled definition names were collected"
    assert "default" not in names
    # Whole words, case-sensitive: a name must not touch another identifier character.
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    matcher = re.compile(rf"(?<![A-Za-z0-9_])(?:{alternatives})(?![A-Za-z0-9_])")
    # Self-check: the matcher flags a collected name placed in a line.
    sample = sorted(names)[0]
    assert matcher.search(f"x {sample} y") is not None

    base = _load_json(INVENTORY)["base"]["commit"]
    added = list(_added_lines(base))
    assert added, "the R0 scan paths carry no added lines, so the scan is vacuous"
    violations = [f"{rel}: {text.strip()}" for rel, text in added if matcher.search(text)]
    assert violations == []
