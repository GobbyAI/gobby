"""Regression tests for Python skill validation guidance."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PYTHON_SKILL_ROOT = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/skills/python"
PYTHON_SKILL = PYTHON_SKILL_ROOT / "SKILL.md"
PYTHON_TESTING_REFERENCE = PYTHON_SKILL_ROOT / "references/testing.md"


def test_python_skill_separates_format_fix_from_verification_evidence() -> None:
    router = PYTHON_SKILL.read_text(encoding="utf-8")
    testing = PYTHON_TESTING_REFERENCE.read_text(encoding="utf-8")

    assert '`get_skill_file(name="python", path="references/testing.md")`' in router
    assert "Apply formatter fixes with:" in testing
    assert "uv run ruff format <files>" in testing
    assert "Collect non-mutating completion evidence with:" in testing
    assert "uv run ruff format --check <files>" in testing
    assert "uv run ruff check <files>" in testing
    assert "GOBBY_TEST_PROTECT=1 uv run pytest <tests> -q" in testing
    assert "ruff format . && ruff check . --fix" not in router
    assert "ruff format . && ruff check . --fix" not in testing
