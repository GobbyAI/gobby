"""Tests for canonical Gobby filesystem paths."""

from pathlib import Path

import pytest

from gobby.paths import (
    get_global_agents_dir,
    get_global_mcp_servers_dir,
    get_global_mcp_templates_dir,
    get_global_pipelines_dir,
    get_global_rules_dir,
    get_global_variables_dir,
    get_global_workflows_dir,
    get_gobby_home,
    get_project_mcp_servers_dir,
    get_project_mcp_templates_dir,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("configured_home", [None, "", " ", "\t\n"])
def test_unset_or_blank_gobby_home_uses_user_home(
    configured_home: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user-home"
    working_dir = tmp_path / "working-dir"
    user_home.mkdir()
    working_dir.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.chdir(working_dir)
    if configured_home is None:
        monkeypatch.delenv("GOBBY_HOME", raising=False)
    else:
        monkeypatch.setenv("GOBBY_HOME", configured_home)

    expected_home = user_home / ".gobby"
    workflows_dir = expected_home / "workflows"

    assert get_gobby_home() == expected_home
    assert get_global_workflows_dir() == workflows_dir
    assert get_global_rules_dir() == workflows_dir / "rules"
    assert get_global_pipelines_dir() == workflows_dir / "pipelines"
    assert get_global_agents_dir() == workflows_dir / "agents"
    assert get_global_variables_dir() == workflows_dir / "variables"
    assert get_global_mcp_templates_dir() == expected_home / "mcp" / "templates"
    assert get_global_mcp_servers_dir() == expected_home / "mcp" / "servers"
    assert (
        get_project_mcp_templates_dir(working_dir) == working_dir / ".gobby" / "mcp" / "templates"
    )
    assert get_project_mcp_servers_dir(working_dir) == working_dir / ".gobby" / "mcp" / "servers"
    assert all(
        not path.is_relative_to(working_dir)
        for path in (
            get_gobby_home(),
            get_global_workflows_dir(),
            get_global_rules_dir(),
            get_global_pipelines_dir(),
            get_global_agents_dir(),
            get_global_variables_dir(),
            get_global_mcp_templates_dir(),
            get_global_mcp_servers_dir(),
        )
    )


def test_explicit_gobby_home_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured_home = tmp_path / "configured-home"
    monkeypatch.setenv("GOBBY_HOME", str(configured_home))

    assert get_gobby_home() == configured_home
    assert get_global_workflows_dir() == configured_home / "workflows"


def test_explicit_tilde_gobby_home_is_expanded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GOBBY_HOME", "~/configured-home")

    assert get_gobby_home() == tmp_path / "configured-home"
