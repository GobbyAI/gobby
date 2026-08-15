"""Grep-style audit that removed workflow-definition storage cannot return."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

TOKENS: tuple[str, ...] = (
    "workflow_definitions",
    "workflow_instances",
    "workflow_states",
    "LocalWorkflowDefinitionManager",
    "WorkflowDefinitionRow",
    "workflow_type",
    "register_agent_step_workflow",
    "_step_workflow_name",
    "/api/workflows",
)

# Exact (path, token, reason). A stale entry fails test_allowlist_is_self_pruning.
ALLOWLIST: tuple[tuple[str, str, str], ...] = ()

# Plan 7.2 owner inventory. Each pair must be absent or allowlisted.
OWNER_INVENTORY: tuple[tuple[str, str], ...] = (
    ("src/gobby/config/tasks.py", "workflow_states"),
    ("src/gobby/workflows/template_hashes.py", "workflow_definitions"),
    ("src/gobby/storage/skills/_metadata.py", "workflow_definitions"),
    ("src/gobby/agents/terminal_cleanup.py", "workflow_instances"),
    ("src/gobby/servers/middleware/project_context.py", "/api/workflows"),
    ("src/gobby/workflows/dry_run.py", "workflow_type"),
    ("src/gobby/workflows/engine/evaluation.py", "WorkflowDefinitionRow"),
    ("src/gobby/workflows/reserved_variables.py", "WorkflowDefinitionRow"),
    ("src/gobby/mcp_proxy/tools/agents_termination.py", "workflow_instances"),
    ("src/gobby/workflows/workflow_templates.py", "workflow_type"),
    ("crates/gcore/assets/schema/baseline.sql", "workflow_type"),
)

_SHARED_SUFFIXES = {".yaml", ".yml", ".md"}
_WEB_SUFFIXES = {".ts", ".tsx"}
_AGENT_TOP_LEVEL_FORBIDDEN = ("steps", "step_variables", "exit_condition")

_STEPS_NAME_RE = re.compile(
    r"""
    f["'][^"'\n]*-steps["']
    | ["']-steps(?:\$)?["']
    | ["']%s-steps["']
    | ["']\{\}-steps["']
    | \$\{[^}]+\}-steps
    | <agent>-steps
    | \.endswith\(\s*["']-steps["']\s*\)
    | ~\s*['"]-steps\$['"]
    | \+\s*["']-steps["']
    """,
    re.VERBOSE,
)


def _token_regex(token: str) -> re.Pattern[str]:
    if token == "/api/workflows":
        return re.compile(re.escape(token))
    return re.compile(rf"\b{re.escape(token)}\b")


_TOKEN_REGEXES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (token, _token_regex(token)) for token in TOKENS
)


def _iter_scan_paths() -> Iterator[Path]:
    seen: set[Path] = set()

    def _emit(path: Path) -> Iterator[Path]:
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            return
        seen.add(resolved)
        yield path

    for path in (ROOT / "src" / "gobby").rglob("*.py"):
        yield from _emit(path)

    web_src = ROOT / "web" / "src"
    if web_src.is_dir():
        for path in web_src.rglob("*"):
            if path.suffix in _WEB_SUFFIXES:
                yield from _emit(path)

    baseline = ROOT / "crates" / "gcore" / "assets" / "schema" / "baseline.sql"
    yield from _emit(baseline)

    shared = ROOT / "src" / "gobby" / "install" / "shared"
    if shared.is_dir():
        for path in shared.rglob("*"):
            if path.suffix.lower() in _SHARED_SUFFIXES:
                yield from _emit(path)


def _relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def _scan_hits() -> list[tuple[str, str, int, str]]:
    hits: list[tuple[str, str, int, str]] = []
    for path in _iter_scan_paths():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative = _relative(path)
        for line_number, line in enumerate(text.splitlines(), start=1):
            for token, regex in _TOKEN_REGEXES:
                if regex.search(line):
                    hits.append((relative, token, line_number, line.strip()))
            if _STEPS_NAME_RE.search(line):
                hits.append((relative, "-steps", line_number, line.strip()))
    return hits


def _file_has_token(relative: str, token: str) -> bool:
    path = ROOT / relative
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if token == "-steps":
        return any(_STEPS_NAME_RE.search(line) for line in text.splitlines())
    return _token_regex(token).search(text) is not None


def test_no_unallowlisted_legacy_tokens() -> None:
    allowlisted = {(path, token) for path, token, _reason in ALLOWLIST}
    unexpected = [
        f"{path}:{line_number}:{token}:{snippet}"
        for path, token, line_number, snippet in _scan_hits()
        if (path, token) not in allowlisted
    ]
    assert unexpected == []


def test_allowlist_is_self_pruning() -> None:
    scanned = {_relative(path) for path in _iter_scan_paths()}
    assert "crates/gcore/assets/schema/baseline.sql" in scanned
    assert any(path.startswith("src/gobby/install/shared/") for path in scanned)
    stale = [
        f"{path}:{token}:{reason}"
        for path, token, reason in ALLOWLIST
        if not _file_has_token(path, token)
    ]
    assert stale == []


def test_every_preexisting_occurrence_has_an_owner() -> None:
    allowlisted = {(path, token) for path, token, _reason in ALLOWLIST}
    unowned = [
        f"{path}:{token}"
        for path, token in OWNER_INVENTORY
        if _file_has_token(path, token) and (path, token) not in allowlisted
    ]
    assert unowned == []


def test_bundled_agent_yaml_has_no_top_level_step_keys() -> None:
    agents_dir = ROOT / "src" / "gobby" / "install" / "shared" / "workflows" / "agents"
    violations: list[str] = []
    for path in sorted(agents_dir.glob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            violations.append(f"{path.name}: not a mapping")
            continue
        present = [key for key in _AGENT_TOP_LEVEL_FORBIDDEN if key in loaded]
        if present:
            violations.append(f"{path.name}:{','.join(present)}")
    assert violations == []
