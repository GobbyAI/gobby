"""Filesystem ownership boundaries for provider router installation."""

from pathlib import Path

import click
import pytest

from gobby.cli.installers.skill_install import (
    install_router_skills_as_cli_skills,
    install_router_skills_as_commands,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_router_install_honors_live_utf8_limit(tmp_path: Path) -> None:
    from gobby.cli.installers.skill_install import _router_carrier
    from gobby.cli.runtime import CliRuntime
    from gobby.config.app import DaemonConfig
    from gobby.config.skills import SkillsConfig

    source = tmp_path / "SKILL.md"
    source.write_text("界" * 600, encoding="utf-8")
    runtime = CliRuntime(
        None, config=DaemonConfig(skills=SkillsConfig(bundled_max_content_size=1000))
    )
    with click.Context(click.Command("install"), obj=runtime):
        with pytest.raises(ValueError, match="limit 1000"):
            _router_carrier(source)
    assert source.read_text(encoding="utf-8") == "界" * 600


def test_empty_custom_alias_directory_is_preserved(tmp_path: Path) -> None:
    alias = tmp_path / "g"
    alias.mkdir()
    assert install_router_skills_as_cli_skills(tmp_path) == ["gobby/"]
    assert alias.is_dir()


@pytest.mark.parametrize("layout", ["commands", "skills"])
@pytest.mark.parametrize(
    "revision", ["e0eed4ddaf", "acf2634373", "ec5536f254-generated", "dc1a751129-rendered"]
)
def test_verified_router_upgrade_is_repeatable(tmp_path: Path, layout: str, revision: str) -> None:
    target = tmp_path / ("gobby.md" if layout == "commands" else "gobby/SKILL.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((FIXTURES / f"gobby-{revision}.md").read_bytes())
    install = (
        install_router_skills_as_commands
        if layout == "commands"
        else install_router_skills_as_cli_skills
    )
    assert install(tmp_path) == (["gobby.md"] if layout == "commands" else ["gobby/"])
    upgraded = target.read_bytes()
    assert b"## Available Capabilities" not in upgraded
    assert b"Make zero tool calls" in upgraded
    assert len(upgraded) <= 15_000
    assert install(tmp_path)
    assert target.read_bytes() == upgraded


@pytest.mark.parametrize("layout", ["commands", "skills"])
def test_custom_router_and_alias_are_preserved(
    tmp_path: Path, layout: str, caplog: pytest.LogCaptureFixture
) -> None:
    paths = (
        [tmp_path / "gobby.md", tmp_path / "g.md"]
        if layout == "commands"
        else [tmp_path / "gobby/SKILL.md", tmp_path / "g/SKILL.md"]
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("user instructions")
    install = (
        install_router_skills_as_commands
        if layout == "commands"
        else install_router_skills_as_cli_skills
    )
    assert install(tmp_path) == []
    for path in paths:
        assert path.read_text() == "user instructions"
    assert str(paths[0]) in caplog.text
    assert "move it aside" in caplog.text


@pytest.mark.parametrize("layout", ["commands", "skills"])
@pytest.mark.parametrize("failure", ["custom", "missing-source", "write-error"])
def test_failed_router_install_preserves_previous_alias(
    tmp_path: Path, layout: str, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = tmp_path / ("g.md" if layout == "commands" else "g/SKILL.md")
    alias.parent.mkdir(parents=True, exist_ok=True)
    original = (FIXTURES / "g-f7cfb46e9a.md").read_bytes()
    alias.write_bytes(original)
    target = tmp_path / ("gobby.md" if layout == "commands" else "gobby/SKILL.md")
    if failure == "custom":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("user instructions")
    elif failure == "missing-source":
        monkeypatch.setattr("gobby.cli.installers.skill_install.get_install_dir", lambda: tmp_path)
    else:

        def deny_write(self: Path, *args: object, **kwargs: object) -> int:
            raise PermissionError("read-only provider directory")

        monkeypatch.setattr(Path, "write_text", deny_write)
    install = (
        install_router_skills_as_commands
        if layout == "commands"
        else install_router_skills_as_cli_skills
    )
    assert install(tmp_path) == []
    assert alias.read_bytes() == original


def test_custom_file_at_skill_directory_is_preserved(tmp_path: Path) -> None:
    target = tmp_path / "gobby"
    target.write_text("user content")
    assert install_router_skills_as_cli_skills(tmp_path) == []
    assert target.read_text() == "user content"


def test_verified_alias_cleanup_preserves_other_files(tmp_path: Path) -> None:
    alias = tmp_path / "g/SKILL.md"
    alias.parent.mkdir()
    alias.write_bytes((FIXTURES / "g-f7cfb46e9a.md").read_bytes())
    extra = alias.parent / "user-notes.md"
    extra.write_text("keep")
    assert install_router_skills_as_cli_skills(tmp_path) == ["gobby/"]
    assert not alias.exists()
    assert extra.read_text() == "keep"


@pytest.mark.parametrize("layout", ["commands", "skills"])
def test_verified_alias_is_removed(tmp_path: Path, layout: str) -> None:
    alias = tmp_path / ("g.md" if layout == "commands" else "g/SKILL.md")
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.write_bytes((FIXTURES / "g-f7cfb46e9a.md").read_bytes())
    install = (
        install_router_skills_as_commands
        if layout == "commands"
        else install_router_skills_as_cli_skills
    )
    assert install(tmp_path)
    assert not alias.exists()
    if layout == "skills":
        assert not alias.parent.exists()


@pytest.mark.parametrize("name", ["g", "gobby"])
def test_skill_directory_symlinks_are_preserved(tmp_path: Path, name: str) -> None:
    external = tmp_path / "external"
    external.mkdir()
    original = (FIXTURES / "gobby-e0eed4ddaf.md").read_bytes()
    (external / "SKILL.md").write_bytes(original)
    skills = tmp_path / "skills"
    skills.mkdir()
    link = skills / name
    link.symlink_to(external, target_is_directory=True)
    result = install_router_skills_as_cli_skills(skills)
    assert result == ([] if name == "gobby" else ["gobby/"])
    assert link.is_symlink()
    assert (external / "SKILL.md").read_bytes() == original


@pytest.mark.parametrize("name", ["g", "gobby"])
def test_command_symlinks_are_preserved(tmp_path: Path, name: str) -> None:
    external = tmp_path / "external.md"
    original = (FIXTURES / "gobby-e0eed4ddaf.md").read_bytes()
    external.write_bytes(original)
    link = tmp_path / f"{name}.md"
    link.symlink_to(external)
    result = install_router_skills_as_commands(tmp_path)
    assert result == ([] if name == "gobby" else ["gobby.md"])
    assert link.is_symlink()
    assert external.read_bytes() == original
