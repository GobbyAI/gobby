"""Regression tests for Python skill validation guidance."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PYTHON_SKILL = Path("src/gobby/install/shared/skills/python/SKILL.md")


def test_python_skill_separates_format_fix_from_verification_evidence() -> None:
    body = PYTHON_SKILL.read_text(encoding="utf-8")

    assert "`uv run ruff format <files>`" in body
    assert "`uv run ruff format --check <files>`" in body
    assert "`uv run ruff check <files>`" in body
    assert "ruff format . && ruff check . --fix" not in body
