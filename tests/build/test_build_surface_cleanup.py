"""Tests for the cleaned build option surface."""

from typing import Any, cast

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit


def test_build_options_includes_profiles_and_omits_yolo_fields() -> None:
    from gobby.build.service import BuildOptions

    fields = set(getattr(BuildOptions, "__dataclass_fields__", {}))
    assert {"profile", "unattended", "quick", "no_merge", "pr"}.issubset(fields)
    assert {
        "stages",
        "add_stages",
        "yolo",
        "composer_yolo",
    }.isdisjoint(fields)


def test_http_build_options_tracks_profile_and_unattended_explicitness() -> None:
    from gobby.servers.routes.build import BuildRequest, _build_options

    default_opts = _build_options(BuildRequest(input_ref="#42"))
    assert default_opts.profile == "default"
    assert default_opts.profile_explicit is False
    assert default_opts.unattended is False
    assert default_opts.unattended_explicit is False

    explicit_opts = _build_options(BuildRequest(input_ref="#42", profile="submit", unattended=True))
    assert explicit_opts.profile == "submit"
    assert explicit_opts.profile_explicit is True
    assert explicit_opts.unattended is True
    assert explicit_opts.unattended_explicit is True


def test_quick_and_no_merge_flags_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.cli.build import build_command

    captured: dict[str, object] = {}

    async def fake_build(input_ref: str, opts: object, **kwargs: object) -> object:
        from gobby.build.service import BuildResult

        captured["input_ref"] = input_ref
        captured["opts"] = opts
        return BuildResult(
            task_id="task-1",
            created=False,
            initial_lifecycle="development",
            applied_stages_skipped=[],
            tick_dispatched=0,
        )

    monkeypatch.setattr("gobby.cli.build.resolve_project_id", lambda: "project-1")
    monkeypatch.setattr("gobby.cli.build.LocalDatabase", lambda: _ClosableDb())
    monkeypatch.setattr("gobby.cli.build.run_migrations", lambda _db: 0)
    monkeypatch.setattr("gobby.cli.build._try_daemon_build", lambda *_args: None)
    monkeypatch.setattr("gobby.cli.build.build", fake_build)

    result = CliRunner().invoke(
        build_command,
        [
            "#42",
            "--quick",
            "--no-merge",
            "--stage",
            "development:max_review_rounds=2",
        ],
    )

    assert result.exit_code == 0
    opts = cast(Any, captured["opts"])
    assert opts.quick is True
    assert opts.no_merge is True
    assert opts.stage_caps[0].stage_name == "development"
    assert opts.stage_caps[0].max_review_rounds == 2


def test_json_surfaces_omit_removed_fields(temp_db: Any) -> None:
    from unittest.mock import MagicMock

    from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
    from gobby.servers.routes.build import BuildRequest
    from gobby.storage.tasks import LocalTaskManager

    registry = create_task_ops_registry(
        LocalTaskManager(temp_db),
        sync_manager=MagicMock(),
        config=MagicMock(),
    )
    tool = next(item for item in registry.list_tools() if item["name"] == "build_task")
    schema = cast(dict[str, Any], tool["inputSchema"])
    assert {"isolation", "quick", "no_merge", "stage", "pr"}.issubset(schema["properties"])
    assert {
        "profile",
        "stages",
        "add_stages",
        "unattended",
        "yolo",
        "composer_yolo",
    }.isdisjoint(schema["properties"])
    assert {"profile", "unattended", "isolation", "quick", "no_merge", "stage", "pr"}.issubset(
        BuildRequest.model_fields
    )


def test_resolve_build_isolation_accepts_legacy_clone_flag() -> None:
    from gobby.build.options import resolve_build_isolation

    resolved = resolve_build_isolation(isolation=None, workspace_backend=None, clone=True)

    assert resolved.isolation == "clone"
    assert resolved.explicit is True


@pytest.mark.parametrize(
    ("isolation", "workspace_backend", "clone", "message"),
    [
        ("worktree", None, True, "clone=true conflicts with isolation=worktree"),
        (None, "worktree", True, "clone=true conflicts with workspace_backend=worktree"),
        ("clone", "worktree", False, "isolation conflicts with workspace_backend"),
    ],
)
def test_resolve_build_isolation_rejects_conflicts(
    isolation: str | None,
    workspace_backend: str | None,
    clone: bool,
    message: str,
) -> None:
    from typing import cast

    from gobby.build.options import resolve_build_isolation
    from gobby.build.workspaces import WorkspaceBackend
    from gobby.config.build import Isolation

    with pytest.raises(ValueError, match=message):
        resolve_build_isolation(
            isolation=cast(Isolation | None, isolation),
            workspace_backend=cast(WorkspaceBackend | None, workspace_backend),
            clone=clone,
        )


class _ClosableDb:
    def close(self) -> None:
        pass
