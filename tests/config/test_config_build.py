"""Tests for build configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def test_build_config_defaults_include_agent_limit() -> None:
    from gobby.config.build import SKIPPABLE_STAGES, BuildConfig

    cfg = BuildConfig()

    assert SKIPPABLE_STAGES == frozenset(
        {
            "ideation",
            "research",
            "architecture",
            "prd",
            "planning",
            "expansion",
            "development",
            "epic_qa",
            "pr",
            "merge",
        }
    )
    assert cfg.max_active_agents == 10


def test_load_build_config_merges_agent_limit_from_global_project_and_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.config import build as build_config

    home = tmp_path / "home"
    project_root = tmp_path / "project"
    (home / ".gobby").mkdir(parents=True)
    (project_root / ".gobby").mkdir(parents=True)
    (home / ".gobby" / "build.yaml").write_text(yaml.safe_dump({"max_active_agents": 4}))
    monkeypatch.setattr(Path, "home", lambda: home)

    global_only = build_config.load_build_config(project_root=project_root)
    assert global_only.max_active_agents == 4

    (project_root / ".gobby" / "build.yaml").write_text(yaml.safe_dump({"max_active_agents": 3}))
    project_overrides_global = build_config.load_build_config(project_root=project_root)
    assert project_overrides_global.max_active_agents == 3

    cfg = build_config.load_build_config(
        project_root=project_root,
        flag_overrides={"max_active_agents": 2},
    )
    assert cfg.max_active_agents == 2


@pytest.mark.parametrize("invalid_value", [0, -1])
def test_load_build_config_rejects_non_positive_agent_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_value: int,
) -> None:
    from gobby.config import build as build_config

    home = tmp_path / "home"
    project_root = tmp_path / "project"
    (project_root / ".gobby").mkdir(parents=True)
    (project_root / ".gobby" / "build.yaml").write_text(
        yaml.safe_dump({"max_active_agents": invalid_value})
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    with pytest.raises(
        ValueError,
        match=r"^max_active_agents must be greater than or equal to 1$",
    ):
        build_config.load_build_config(project_root=project_root)
