"""Bundled seat definitions, checked from their YAML without naming any seat."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.workflows.agent_models import AgentDefinitionBody

pytestmark = pytest.mark.unit

TDD_SKILL = "test-driven-development"
TEST_BEFORE = "test-before"
CODE_BEARING_CATEGORIES = {"code", "config", "refactor", "test"}


def _bundled_files() -> list[Path]:
    return sorted(get_bundled_agents_path().glob("*.yaml"))


def _raw(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path
    return data


def _body(path: Path) -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(_raw(path))


def test_a_spawner_guard_is_bundled() -> None:
    guarded = [path for path in _bundled_files() if _body(path).spawner_capabilities]
    assert guarded, "the bundled roster must include a spawner-guarded definition"


@pytest.mark.parametrize("path", _bundled_files(), ids=lambda path: path.stem)
def test_spawner_guarded_definitions_are_spawn_only(path: Path) -> None:
    body = _body(path)
    if not body.spawner_capabilities:
        return
    assert body.supports_surface("spawn")
    assert not body.supports_surface("persona")
    assert not body.supports_cold_start()
    assert not body.cold_start_commands
    assert body.cold_start_counterpart is None


@pytest.mark.parametrize("path", _bundled_files(), ids=lambda path: path.stem)
def test_spawner_guard_admits_only_callers_holding_a_listed_capability(path: Path) -> None:
    guard = _body(path)
    if not guard.spawner_capabilities:
        return
    for caller_path in _bundled_files():
        caller = _body(caller_path)
        holds = bool(set(caller.capabilities) & set(guard.spawner_capabilities))
        assert guard.admits_spawner(caller.capabilities) is holds, caller_path.stem


def _writes_code_test_before(raw: dict[str, Any]) -> bool:
    """Code-bearing writers own red-green evidence; docs-only writers record non-applicability."""
    return TEST_BEFORE in (raw.get("task_attestation_writes") or []) and bool(
        CODE_BEARING_CATEGORIES & set(raw.get("task_categories") or [])
    )


def test_a_code_test_before_writer_is_bundled() -> None:
    writers = [path for path in _bundled_files() if _writes_code_test_before(_raw(path))]
    assert writers, "the bundled roster must include a code test-before writer"


@pytest.mark.parametrize("path", _bundled_files(), ids=lambda path: path.stem)
def test_code_test_before_writers_load_the_tdd_skill(path: Path) -> None:
    raw = _raw(path)
    if not _writes_code_test_before(raw):
        return
    variables = (raw.get("workflows") or {}).get("variables") or {}
    assert TDD_SKILL in (variables.get("_seat_references") or [])
