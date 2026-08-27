"""Regression tests for bundled skill commit message guidance."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def bundled_skills_root(repo_root: Path) -> Path:
    return repo_root / "src/gobby/install/shared/skills"


def test_commit_message_guidance_uses_project_placeholder(
    bundled_skills_root: Path,
) -> None:
    skill_bodies = {
        name: (bundled_skills_root / name / "SKILL.md").read_text(encoding="utf-8")
        for name in ("coderabbit", "tasks", "source-control")
    }
    for skill_name, body in skill_bodies.items():
        assert "[<project_name>-#<task_number>]" in body
        assert "[gobby-#" not in body
        assert "[gobby-cli-#" not in body
        git_commit_examples = re.findall(r"git commit(?: --only)? -m \"([^\"]+)\"", body)
        if skill_name == "coderabbit":
            assert "Commit with the task ref" in body
        else:
            assert git_commit_examples, f"{skill_name} must include a git commit example"
        assert not any("[#N]" in example for example in git_commit_examples)


def test_tasks_skill_scopes_commits_to_owned_task_paths(bundled_skills_root: Path) -> None:
    body = (bundled_skills_root / "tasks" / "SKILL.md").read_text(encoding="utf-8")

    assert "git commit --only -- <task paths>" in body
    assert 'git commit --only -m "[<project_name>-#<task_number>]' in body
    assert "foreign staged entries remain intact" in body
