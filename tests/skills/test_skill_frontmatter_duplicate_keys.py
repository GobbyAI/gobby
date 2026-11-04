"""A SKILL.md front matter with a duplicate mapping key is refused, never last-wins."""

from pathlib import Path

import pytest

from gobby.skills.parser import SkillParseError, parse_skill_text
from gobby.skills.sync import sync_bundled_skills
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills import LocalSkillManager

pytestmark = pytest.mark.unit

_GOOD_FRONTMATTER = """---
name: {name}
description: {description}
metadata:
  gobby:
    audience: all
---
"""

_DUPLICATE_FRONTMATTER = """---
name: dup-skill
description: Changed.
description: Second.
metadata:
  gobby:
    audience: all
---
"""


def _skill_md(name: str, description: str) -> str:
    return _GOOD_FRONTMATTER.format(name=name, description=description) + f"\n# {name}\n"


def _write_skill(root: Path, name: str, text: str) -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(text)


def test_parse_refuses_duplicate_frontmatter_key() -> None:
    text = _DUPLICATE_FRONTMATTER + "\n# dup\n"

    with pytest.raises(SkillParseError, match="duplicate mapping key"):
        parse_skill_text(text)


def test_duplicate_key_skill_refused_while_sibling_syncs_and_installed_row_survives(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gobby.skills.sync.get_bundled_skills_path", lambda: tmp_path)
    _write_skill(tmp_path, "dup-skill", _skill_md("dup-skill", "First."))
    assert sync_bundled_skills(temp_db)["success"] is False

    manager = LocalSkillManager(temp_db)
    kept = manager.get_by_name("dup-skill", project_id=None)
    assert kept is not None
    assert kept.description == "First."

    _write_skill(tmp_path, "dup-skill", _DUPLICATE_FRONTMATTER + "\n# dup\n")
    _write_skill(tmp_path, "fresh-skill", _skill_md("fresh-skill", "New."))

    result = sync_bundled_skills(temp_db)

    assert result["success"] is False, result
    assert any(
        "dup-skill" in error and "duplicate mapping key" in error for error in result["errors"]
    ), result
    kept = manager.get_by_name("dup-skill", project_id=None)
    assert kept is not None
    assert kept.description == "First."
    assert manager.get_by_name("fresh-skill", project_id=None) is not None
