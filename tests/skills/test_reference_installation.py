"""Rehearse the final reference bundle against isolated storage and provider homes."""

import hashlib
from pathlib import Path

import pytest

from gobby.cli.installers.skill_install import (
    install_router_skills_as_cli_skills,
    install_router_skills_as_commands,
)
from gobby.install.manifest import build_bundled_content_manifest
from gobby.skills.capability_catalog import load_capability_catalog
from gobby.skills.sync import get_bundled_skills_path, sync_bundled_skills
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills import LocalSkillManager

STANDALONE = set(
    "brevity restraint proportionality elicit ideate research architecture prd "
    "code-review decompose-monolith repository-maintenance test-driven-development "
    "triage-judgment impeccable tech-writer bridge browser-testing coderabbit context7 "
    "bash c cpp csharp dart elixir go java javascript json kotlin lua objc php python "
    "ruby rust scala swift typescript yaml".split()
)


@pytest.mark.unit
def test_reference_contract_5_2_1() -> None:
    root = get_bundled_skills_path()
    catalog = load_capability_catalog(root / "gobby")
    assert len(catalog.folded_skills) == 30
    assert len(STANDALONE) == 40
    assert {path.parent.name for path in root.glob("*/SKILL.md")} == STANDALONE | {
        "gobby",
        "annotate",
    }
    for name, identity in catalog.folded_skills.items():
        assert not (root / name).exists(), name
        assert (root / "gobby" / identity.split(":", 1)[1]).is_file(), identity
    assert not (root / "gusto").exists()  # User-owned integration is never bundled.


@pytest.mark.integration
@pytest.mark.parametrize("upgrade", [False, True], ids=["fresh", "upgrade"])
def test_reference_contract_5_2_2(temp_db: HubDatabase, tmp_path: Path, upgrade: bool) -> None:
    root = get_bundled_skills_path()
    catalog = load_capability_catalog(root / "gobby")
    skills = LocalSkillManager(temp_db)
    defaults = SessionVariableDefaultManager(temp_db)
    custom = skills.create_skill(
        name="gusto",
        description="Private fixture",
        content="Private reusable integration",
        source="local",
        metadata={"owner": "user"},
    )
    user_requirement = defaults.create(
        "additional_skills", ["tasks", "gusto"], source="installed", tags=["custom"]
    )
    if upgrade:
        for name in catalog.folded_skills:
            skills.create_skill(
                name=name,
                description="Previous bundled entrypoint",
                content="Old instructions",
                source="installed",
                metadata={"gobby": {"audience": "all"}},
            )
    requirement = defaults.create("required_skills", ["tasks", "memory", "brevity"], tags=["gobby"])
    result = sync_bundled_skills(temp_db)
    assert result["success"] is True, result["errors"]
    assert result["orphaned"] == (30 if upgrade else 0)
    assert defaults.get(requirement.id).default_value == [
        "gobby:references/tasks/overview.md",
        "gobby:references/memory/overview.md",
        "brevity",
    ]
    assert defaults.get(user_requirement.id).default_value == ["tasks", "gusto"]
    assert any(user_requirement.id in warning for warning in result["warnings"])
    assert skills.get_by_name("gusto") == custom
    for name in catalog.folded_skills:
        assert skills.get_by_name(name) is None
    router = skills.get_by_name("gobby")
    assert router is not None and router.source == "installed"
    assert router.metadata is not None and "gobby" in router.metadata
    installed = {
        item.path: item.content for item in skills.get_skill_files(router.id, include_content=True)
    }
    expected = {
        path.relative_to(root / "gobby").as_posix(): path.read_text()
        for path in (root / "gobby/references").rglob("*.md")
    }
    assert expected
    assert {path: installed[path] for path in expected} == expected
    for name in STANDALONE | {"annotate"}:
        assert skills.get_by_name(name) is not None
    second = sync_bundled_skills(temp_db)
    assert second["success"] is True, second["errors"]
    assert second["orphaned"] == 0
    assert second["requirements_updated"] == 0
    reloaded_router = skills.get_by_name("gobby")
    assert reloaded_router is not None
    assert reloaded_router.id == router.id
    assert skills.get_by_name("gusto") == custom
    assert {
        item.path: item.content for item in skills.get_skill_files(router.id, include_content=True)
    } == installed

    commands = tmp_path / ".claude/commands"
    native = tmp_path / ".codex/skills"
    custom_file = native / "custom/SKILL.md"
    custom_file.parent.mkdir(parents=True)
    custom_file.write_text("User provider skill")
    for _ in range(2):
        assert install_router_skills_as_commands(commands) == ["gobby.md"]
        assert install_router_skills_as_cli_skills(native) == ["gobby/"]
        assert custom_file.read_text() == "User provider skill"
    for carrier in (commands / "gobby.md", native / "gobby/SKILL.md"):
        content = carrier.read_text()
        assert "get_skill" in content
        assert "tasks" in content
        assert "references" in content
    manifest = build_bundled_content_manifest(root.parent)
    for path in (root / "gobby").rglob("*"):
        if path.is_file():
            assert (
                manifest["files"][path.relative_to(root.parent).as_posix()]
                == hashlib.sha256(path.read_bytes()).hexdigest()
            )
    assert not any(
        path.startswith(f"skills/{name}/")
        for name in catalog.folded_skills
        for path in manifest["files"]
    )
