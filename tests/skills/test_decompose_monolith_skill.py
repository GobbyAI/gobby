"""Contract tests for the bundled decompose-monolith skill."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.skills.loader import SkillLoader
from tests.skills.scenario_runner import run_recorded_skill_scenario

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / "src" / "gobby" / "install" / "shared" / "skills"
SKILL_DIR = SKILLS_ROOT / "decompose-monolith"
SKILL_FILE = SKILL_DIR / "SKILL.md"
REFERENCE_FILE = SKILL_DIR / "references" / "architectural-shapes.md"
SCENARIOS = Path(__file__).resolve().parent / "scenarios" / "decompose-monolith"


def _guidance() -> str:
    return f"{SKILL_FILE.read_text()}\n{REFERENCE_FILE.read_text()}"


def _frontmatter() -> dict[str, object]:
    _, raw, _ = SKILL_FILE.read_text().split("---", 2)
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    return parsed


def test_skill_loads_with_engineering_metadata_and_reference() -> None:
    parsed = SkillLoader().load_skill(SKILL_DIR, validate=True)

    assert parsed.name == "decompose-monolith"
    assert parsed.version == "1.0.0"
    assert parsed.get_category() == "engineering"
    assert parsed.triggers is not None
    assert {
        "oversized source file",
        "monolith",
        "god file",
        "module decomposition",
    }.issubset(parsed.triggers)
    assert parsed.loaded_files is not None
    references = {file.path for file in parsed.loaded_files if file.file_type == "reference"}
    assert references == {"references/architectural-shapes.md"}


def test_skill_discovery_and_contract_cover_projected_same_task_decomposition() -> None:
    frontmatter = _frontmatter()
    description = str(frontmatter["description"]).lower()
    body = SKILL_FILE.read_text()
    normalized_body = " ".join(body.split())

    assert "projected to cross" in description
    assert "current claimed feature or fix task" in body
    assert "must finish in the current session" in body
    assert "never waives the line ceiling" in body
    assert "Check both current and projected line counts" in normalized_body
    assert "exactly the ceiling is a violation" in normalized_body
    assert "Deferred refactor tasks are prohibited" in body


def test_bundled_loader_is_the_discovery_surface() -> None:
    loaded = SkillLoader().load_directory(SKILLS_ROOT)

    assert "decompose-monolith" in {skill.name for skill in loaded}


def test_skill_attributes_the_three_refactoring_sources() -> None:
    sources = _frontmatter().get("sources")

    assert isinstance(sources, list)
    source_text = " ".join(str(source) for source in sources)
    assert "affaan-m/ecc" in source_text
    assert "github/awesome-copilot" in source_text
    assert "wshobson/agents" in source_text


def test_skill_requires_code_index_and_uses_language_neutral_graph_analysis() -> None:
    guidance = _guidance()

    assert "REQUIRED SKILL: code-index." in guidance
    for command in (
        "gcode outline <file>",
        "gcode imports <file>",
        "gcode usages <symbol-id>",
        "gcode callers <symbol-id>",
        "gcode blast-radius <symbol-name>",
        "gcode search-symbol",
        "gcode grep",
        "gcode search-content",
    ):
        assert command in guidance
    for language_or_framework in ("Python", "TypeScript", "React", "Django", "Rust"):
        assert language_or_framework not in guidance


def test_workflow_defines_scope_characterization_and_acyclic_boundaries() -> None:
    body = SKILL_FILE.read_text()

    ordered_phrases = (
        "### 1. Confirm Scope and Ceiling",
        "### 2. Establish a Green Characterization Baseline",
        "### 3. Map Structure and Consumers with `gcode`",
        "### 4. Discover Cohesive Boundaries",
        "### 5. Design the Target Module Graph",
        "### 6. Select the Migration Strategy",
        "### 7. Extract Cohesive Slices",
    )
    positions = [body.index(phrase) for phrase in ordered_phrases]
    assert positions == sorted(positions)
    assert "If the repository defines none, use 1,000 lines." in body
    for excluded in (
        "generated",
        "vendored",
        "baseline",
        "documentation",
        "fixture",
        "test artifacts",
    ):
        assert excluded in body
    for dimension in ("responsibility", "state ownership", "reason to change", "consumers"):
        assert dimension in body
    assert "record a green baseline" in body
    assert "The graph must be acyclic" in body
    assert "Direct extraction is the default." in body


def test_strategy_contract_makes_strangler_cleanup_post_verification() -> None:
    body = SKILL_FILE.read_text()
    strangler = body.split("## Strangler Migration", 1)[1]

    assert "old and new" in body
    assert "paths must coexist" in body
    assert "callers and imports can migrate atomically" in body
    verify_position = strangler.index("Verify every consumer uses the new path")
    cleanup_position = strangler.index("Perform mandatory post-verification cleanup")
    final_position = strangler.index("Run a final green validation after cleanup")
    assert verify_position < cleanup_position < final_position
    for cleanup in (
        "delete the legacy implementation",
        "temporary routers, adapters, feature flags",
        "obsolete tests, configuration, metrics, and documentation",
        "collapse any temporary facade",
    ):
        assert cleanup in strangler
    assert "A strangler decomposition is incomplete" in strangler


def test_structural_completion_gates_reject_shortcuts() -> None:
    body = SKILL_FILE.read_text()

    for criterion in (
        "Every hand-maintained production source file is below the applicable ceiling.",
        "Each resulting file has an independently describable responsibility.",
        "Dependencies are acyclic and flow toward stable domain units.",
        "Shared mutable state has one explicit owner.",
        "Public surfaces remain minimal.",
        "Temporary migration machinery has been removed.",
        "compiler or type checks",
        "relevant runtime checks",
    ):
        assert criterion in body
    for shortcut in (
        "arbitrary line-range extraction",
        "generic `utils` or `common` modules",
        "permanent forwarding shells",
        "circular imports",
        "tiny fragments",
        "near-threshold coordinator",
    ):
        assert shortcut in body


def test_architectural_reference_covers_required_shapes_and_both_strategies() -> None:
    reference = REFERENCE_FILE.read_text()

    for heading in (
        "## Service",
        "## Parser or Compiler",
        "## UI or Component",
        "## Systems Module",
        "## Stylesheet",
        "## Direct Extraction Example",
        "## Strangler Example",
    ):
        assert heading in reference
    assert len(reference.splitlines()) < 120


@pytest.mark.skill_tdd
def test_direct_extraction_scenario_rejects_arbitrary_helpers_split() -> None:
    result = run_recorded_skill_scenario(SCENARIOS / "direct-extraction.yaml")

    assert result.baseline.action_names == (
        "extract_line_range",
        "update_consumers",
        "respond",
    )
    assert result.loaded.action_names == (
        "confirm_source_ceiling",
        "map_behavior_with_gcode",
        "establish_characterization_baseline",
        "group_symbols_by_responsibility",
        "design_acyclic_module_graph",
        "select_direct_extraction",
        "extract_cohesive_slice",
        "update_consumers",
        "run_focused_validation",
        "validate_structure",
        "run_final_validation",
        "respond",
    )
    assert "extract_line_range" not in result.loaded.action_names
    assert result.has_behavioral_delta


@pytest.mark.skill_tdd
def test_strangler_scenario_requires_verified_cutover_cleanup_and_revalidation() -> None:
    result = run_recorded_skill_scenario(SCENARIOS / "strangler-migration.yaml")

    actions = result.loaded.action_names
    assert actions == (
        "define_strangler_plan",
        "introduce_routing_seam",
        "introduce_new_module",
        "migrate_consumers",
        "verify_cutover",
        "post_verification_cleanup",
        "run_final_validation",
        "respond",
    )
    assert actions.index("verify_cutover") < actions.index("post_verification_cleanup")
    assert actions.index("post_verification_cleanup") < actions.index("run_final_validation")
    assert result.has_behavioral_delta
