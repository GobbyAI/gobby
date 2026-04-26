"""Red tests for the Phase 3 build configuration contract."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def test_build_config_defaults_include_profiles_and_dispatch_knobs() -> None:
    from gobby.config.build import SKIPPABLE_STAGES, BuildConfig

    cfg = BuildConfig()

    assert SKIPPABLE_STAGES == frozenset(
        {"plan_review", "test_arch", "expanding", "qa", "holistic_review", "pr"}
    )
    assert cfg.default_skip_stages == ()
    assert cfg.default_isolation == "worktree"
    assert cfg.default_yolo is False
    assert cfg.default_max_review_rounds == 3
    assert cfg.default_target_branch is None
    assert cfg.clones_dir == Path.home() / ".gobby" / "clones"
    assert cfg.cleanup_clones_on_merge is True
    assert cfg.max_active_agents == 10
    assert cfg.dispatch_interval_seconds == 60
    assert cfg.profiles["quick"] == {
        "skip_stages": ["plan_review", "test_arch", "expanding", "qa", "holistic_review", "pr"],
        "isolation": "none",
        "yolo": False,
    }
    assert cfg.profiles["review"] == {
        "skip_stages": ["plan_review", "pr"],
        "isolation": "worktree",
        "yolo": False,
    }
    assert cfg.profiles["full"] == {
        "skip_stages": [],
        "isolation": "worktree",
        "yolo": False,
    }
    assert cfg.profiles["full-yolo"] == {
        "skip_stages": ["pr"],
        "isolation": "worktree",
        "yolo": True,
    }


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
                "default_skip_stages": ["plan_review"],
                "default_isolation": "clone",
                "default_yolo": True,
                "default_max_review_rounds": 5,
                "default_target_branch": "main",
                "clones_dir": str(home / "custom-clones"),
                "max_active_agents": 4,
                "profiles": {
                    "review": {
                        "skip_stages": ["plan_review", "holistic_review", "pr"],
                        "isolation": "clone",
                        "yolo": True,
                    }
                },
            }
        )
    )
    (project_root / ".gobby" / "build.yaml").write_text(
        yaml.safe_dump(
            {
                "default_skip_stages": ["qa"],
                "default_yolo": False,
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

    assert cfg.default_skip_stages == ("qa",)
    assert cfg.default_isolation == "none"
    assert cfg.default_yolo is False
    assert cfg.default_max_review_rounds == 5
    assert cfg.default_target_branch == "release/0.4"
    assert cfg.clones_dir == home / "custom-clones"
    assert cfg.cleanup_clones_on_merge is False
    assert cfg.max_active_agents == 2
    assert cfg.dispatch_interval_seconds == 15
    assert cfg.profiles["review"] == {
        "skip_stages": ["plan_review", "holistic_review", "pr"],
        "isolation": "clone",
        "yolo": True,
    }


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (
            "quick",
            {
                "skip_stages": [
                    "plan_review",
                    "test_arch",
                    "expanding",
                    "qa",
                    "holistic_review",
                    "pr",
                ],
                "isolation": "none",
                "yolo": False,
            },
        ),
        ("review", {"skip_stages": ["plan_review", "pr"], "isolation": "worktree", "yolo": False}),
        ("full", {"skip_stages": [], "isolation": "worktree", "yolo": False}),
        ("full-yolo", {"skip_stages": ["pr"], "isolation": "worktree", "yolo": True}),
    ],
)
def test_resolve_profile_returns_builtin_presets(profile: str, expected: dict[str, object]) -> None:
    from gobby.config.build import BuildConfig, resolve_profile

    assert resolve_profile(BuildConfig(), profile, input_ref="#42") == expected


@pytest.mark.parametrize(
    ("input_ref", "input_kind", "has_plan_file", "expected_profile"),
    [
        ("plan.md", "plan_file", False, "review"),
        ("#101", "leaf", False, "quick"),
        ("#102", "epic", True, "full"),
    ],
)
def test_resolve_profile_auto_maps_input_shape_to_profile(
    input_ref: str,
    input_kind: str,
    has_plan_file: bool,
    expected_profile: str,
) -> None:
    from gobby.config.build import BuildConfig, resolve_profile

    cfg = BuildConfig()

    assert resolve_profile(
        cfg,
        "auto",
        input_ref=input_ref,
        input_kind=input_kind,
        has_plan_file=has_plan_file,
    ) == resolve_profile(cfg, expected_profile, input_ref=input_ref)


def test_resolve_profile_auto_rejects_epic_without_plan_artifact() -> None:
    from gobby.config.build import BuildConfig, resolve_profile

    with pytest.raises(ValueError, match="epic.*plan"):
        resolve_profile(
            BuildConfig(),
            "auto",
            input_ref="#103",
            input_kind="epic",
            has_plan_file=False,
        )
