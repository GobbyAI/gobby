"""Complete loading guidance is split into entrypoint and reference topics."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = (
    Path(__file__).resolve().parents[2] / "src/gobby/install/shared/skills/gobby/references/skills"
)


def test_loading_skills_requires_complete_separate_results_and_retry() -> None:
    content = " ".join((ROOT / "loading.md").read_text().split())
    for expected in (
        "`brief=true`",
        "`page.next_cursor`",
        "with no initial lookup arguments",
        "Every page needs its own outer result",
        "Emit content together with page metadata",
        "never parallelize skill pages or combine full skill results",
        "Deduplicate multiple skills preserving required order",
        "load them sequentially",
        "Only a completed entrypoint records the skill and its effective level",
        "`stale_cursor` or `invalid_cursor`",
        "restart the original lookup",
        "consume every page",
        "content is absent or explicitly truncated",
        "Collapsed UI presentation alone is not truncation",
    ):
        assert expected in content


def test_loading_skills_states_schema_first_reference_loads() -> None:
    content = " ".join((ROOT / "references.md").read_text().split())
    for expected in (
        "schema lease for `get_skill_file` or `get_skill_files`",
        "neither is bootstrap",
        "completed entrypoint's topic index and exact stored path",
        'get_skill_file(name="gobby", path="references/tasks/closing.md")',
        "`page.next_cursor` with only `cursor` until null",
        "one page per outer result",
        "completed reference is tracked separately",
        "loading the router or a menu never satisfies",
        "Context resets clear loaded-reference tracking",
    ):
        assert expected in content
