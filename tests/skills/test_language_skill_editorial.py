"""Editorial contracts shared by the original bundled language-skill family."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from gobby.skills.loader import SkillLoader

pytestmark = pytest.mark.unit

SKILLS_ROOT = Path(__file__).parents[2] / "src/gobby/install/shared/skills"
LANGUAGE_SKILLS = (
    "c",
    "cpp",
    "csharp",
    "dart",
    "elixir",
    "go",
    "java",
    "javascript",
    "json",
    "kotlin",
    "php",
    "python",
    "ruby",
    "rust",
    "swift",
    "typescript",
    "yaml",
)
CONCURRENCY_SKILLS = (
    "cpp",
    "csharp",
    "dart",
    "elixir",
    "go",
    "java",
    "javascript",
    "kotlin",
    "python",
    "ruby",
    "rust",
    "swift",
    "typescript",
)
INLINE_API_DESIGN_SKILLS = ("go", "javascript", "python", "rust", "typescript")
GENERIC_FILLER = (
    "profile before optimizing",
    "measure hot paths before optimizing",
    "test error paths",
    "test failure paths",
    "not only happy paths",
    "never log secrets",
    "before you finish",
)


def _skill_text(name: str) -> str:
    return (SKILLS_ROOT / name / "SKILL.md").read_text()


@pytest.mark.parametrize("name", LANGUAGE_SKILLS)
def test_language_skill_is_concise_specific_and_strictly_loadable(name: str) -> None:
    text = _skill_text(name)
    normalized = " ".join(text.lower().split())

    assert len(text.splitlines()) <= 100
    assert "Diagnostic hook:" in text
    assert not any(phrase in normalized for phrase in GENERIC_FILLER)
    SkillLoader().load_skill(SKILLS_ROOT / name, validate=True)


@pytest.mark.parametrize("name", CONCURRENCY_SKILLS)
def test_language_skill_uses_concurrency_as_the_section_vocabulary(name: str) -> None:
    headings = re.findall(r"^## .+$", _skill_text(name), flags=re.MULTILINE)

    assert "## Concurrency" in headings
    assert all("Async" not in heading and "Coroutines" not in heading for heading in headings)


@pytest.mark.parametrize("name", INLINE_API_DESIGN_SKILLS)
def test_short_api_design_guidance_stays_inline(name: str) -> None:
    text = _skill_text(name)
    section = text.split("## API Design\n", 1)[1].split("\n## ", 1)[0]

    assert re.search(r"^- ", section, flags=re.MULTILINE)
    assert "get_skill_file" not in section


@pytest.mark.parametrize("name", LANGUAGE_SKILLS)
def test_language_skill_headings_use_words_instead_of_ampersands(name: str) -> None:
    headings = re.findall(r"^## .+$", _skill_text(name), flags=re.MULTILINE)

    assert all("&" not in heading for heading in headings)
