"""Contract and pressure tests for the bundled repository-maintenance skill."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.skills.loader import SkillLoader
from tests.skills.scenario_runner import run_recorded_skill_scenario

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED_ROOT = REPO_ROOT / "src" / "gobby" / "install" / "shared"
SKILLS_ROOT = SHARED_ROOT / "skills"
SKILL_DIR = SKILLS_ROOT / "repository-maintenance"
SKILL_FILE = SKILL_DIR / "SKILL.md"
DISCIPLINE_FILE = SKILLS_ROOT / "gobby/references/development/obligations.md"
SCENARIOS = Path(__file__).resolve().parent / "scenarios" / "repository-maintenance"
SCENARIO_CASES = (
    ("shared-helper-placement.yaml", "create_dumping_ground", "identify_capability_owner"),
    ("top-level-package.yaml", "create_one_helper_package", "assess_package_boundary"),
    ("dependency-direction.yaml", "import_adapter_from_domain", "keep_dependency_inward"),
    ("state-ownership.yaml", "duplicate_state_cache", "preserve_single_state_owner"),
    ("large-file-routing.yaml", "split_by_line_range", "delegate_file_decomposition"),
    (
        "generated-content.yaml",
        "manually_update_volatile_inventory",
        "regenerate_separated_content",
    ),
    ("unrelated-cleanup.yaml", "refactor_neighboring_debt", "scope_to_requested_change"),
)


def _frontmatter(path: Path) -> dict[str, object]:
    _, raw, _ = path.read_text().split("---", 2)
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    return parsed


def test_skill_is_public_on_demand_and_discoverable_to_every_agent() -> None:
    parsed = SkillLoader().load_skill(SKILL_DIR, validate=True)

    assert parsed.name == "repository-maintenance"
    assert parsed.version == "1.0.0"
    assert parsed.get_category() == "engineering"
    assert parsed.is_internal() is False
    assert parsed.is_always_apply() is False
    assert parsed.metadata is not None
    assert parsed.metadata["gobby"]["audience"] == "all"
    assert {
        "package creation",
        "module movement",
        "cross-package dependency",
        "shared abstraction",
        "state ownership",
    }.issubset(parsed.triggers or set())
    assert parsed.name in {skill.name for skill in SkillLoader().load_directory(SKILLS_ROOT)}


def test_skill_contract_covers_patterns_anti_patterns_and_maintenance_scope() -> None:
    guidance = " ".join(SKILL_FILE.read_text().lower().split())

    for phrase in (
        "capability-oriented packages",
        "thin adapters",
        "dependencies point inward",
        "one state owner",
        "narrow public api",
        "colocated or mirrored tests",
        "generated and vendor content",
        "utils",
        "common",
        "one-helper package",
        "deep private imports",
        "dependency cycles",
        "duplicated transport and domain models",
        "arbitrary line splits",
        "forwarding facades",
        "manually maintained volatile inventories",
        "do not introduce new structural debt",
        "keep unrelated cleanup outside the task",
        "active repository policy",
    ):
        assert phrase in guidance


def test_development_discipline_keeps_canonical_name_and_adds_structural_preflight() -> None:
    guidance = " ".join(DISCIPLINE_FILE.read_text().split())
    for phrase in (
        "Confirm capability ownership",
        "Resolve named symbols and callers with gcode",
        "dependency direction",
        "state authority",
        "public surfaces",
        "test placement",
        "`repository-maintenance` for package creation/moves",
        "`decompose-monolith` before production decomposition",
    ):
        assert phrase in guidance


def test_repository_maintenance_has_no_automatic_loading_rule() -> None:
    workflow_text = "\n".join(
        path.read_text() for path in (SHARED_ROOT / "workflows").rglob("*.yaml")
    )

    assert "repository-maintenance" not in workflow_text


@pytest.mark.skill_tdd
@pytest.mark.parametrize(("scenario_name", "shortcut", "required_action"), SCENARIO_CASES)
def test_repository_pressure_scenario_changes_structural_judgment(
    scenario_name: str,
    shortcut: str,
    required_action: str,
) -> None:
    SkillLoader().load_skill(SKILL_DIR, validate=True)
    result = run_recorded_skill_scenario(SCENARIOS / scenario_name)

    assert shortcut in result.baseline.action_names
    assert shortcut not in result.loaded.action_names
    assert required_action in result.loaded.action_names
    assert result.has_behavioral_delta
