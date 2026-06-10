"""Contract tests for the bundled Python language skill."""

from pathlib import Path

from gobby.skills.loader import SkillLoader

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/python"


def test_python_skill_parses_with_references() -> None:
    """Verify the bundled Python skill has expected metadata and reference files."""
    parsed = SkillLoader().load_skill(SKILL_DIR, validate=False)

    assert parsed.name == "python"
    assert parsed.version == "1.0.0"
    assert parsed.get_category() == "development"
    assert parsed.triggers is not None
    assert {
        "python",
        "py",
        "pyi",
        "pyproject.toml",
        "uv",
        "ruff",
        "mypy",
        "pytest",
        "tox",
        "nox",
        "typing",
        "asyncio",
    }.issubset(parsed.triggers)
    assert 'get_skill_file(name="python", path="references/configuration.md")' in parsed.content
    assert 'get_skill_file(name="python", path="references/types.md")' in parsed.content
    assert 'get_skill_file(name="python", path="references/error-handling.md")' in (parsed.content)
    assert 'get_skill_file(name="python", path="references/testing.md")' in parsed.content

    assert parsed.loaded_files is not None
    reference_paths = {file.path for file in parsed.loaded_files if file.file_type == "reference"}
    assert reference_paths == {
        "references/async.md",
        "references/configuration.md",
        "references/error-handling.md",
        "references/performance.md",
        "references/testing.md",
        "references/types.md",
    }
