"""Red tests for the Phase 3 build configuration contract."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def test_build_config_defaults_include_dispatch_knobs() -> None:
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
            "holistic_qa",
            "pr",
            "merge",
        }
    )
    assert cfg.default_skip_stages == ()
    assert cfg.default_isolation == "worktree"
    assert cfg.stage_caps == {}
    assert cfg.default_target_branch is None
    assert cfg.clones_dir == Path.home() / ".gobby" / "clones"
    assert cfg.cleanup_clones_on_merge is True
    assert cfg.max_active_agents == 10
    assert cfg.dispatch_interval_seconds == 60


def test_load_build_config_merges_defaults_global_project_and_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.config import build as build_config

    home = tmp_path / "home"
    project_root = tmp_path / "project"
    (home / ".gobby").mkdir(parents=True)
    (project_root / ".gobby").mkdir(parents=True)
    (home / ".gobby" / "build.yaml").write_text(
        yaml.safe_dump(
            {
                "default_skip_stages": ["research"],
                "default_isolation": "clone",
                "default_max_review_rounds": 5,
                "default_target_branch": "main",
                "clones_dir": str(home / "custom-clones"),
                "max_active_agents": 4,
            }
        )
    )
    (project_root / ".gobby" / "build.yaml").write_text(
        yaml.safe_dump(
            {
                "default_skip_stages": ["holistic_qa"],
                "cleanup_clones_on_merge": False,
                "dispatch_interval_seconds": 15,
            }
        )
    )
    monkeypatch.setattr(build_config.Path, "home", lambda: home)

    cfg = build_config.load_build_config(
        project_root=project_root,
        flag_overrides={
            "default_isolation": "none",
            "default_target_branch": "release/0.4",
            "max_active_agents": 2,
        },
    )

    assert cfg.default_skip_stages == ("holistic_qa",)
    assert cfg.default_isolation == "none"
    assert cfg.stage_caps["pr"].max_review_rounds == 5
    assert cfg.stage_caps["pr"].max_work_attempts is None
    assert cfg.default_target_branch == "release/0.4"
    assert cfg.clones_dir == home / "custom-clones"
    assert cfg.cleanup_clones_on_merge is False
    assert cfg.max_active_agents == 2
    assert cfg.dispatch_interval_seconds == 15
