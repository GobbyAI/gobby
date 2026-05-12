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


def test_unknown_profile_exits_with_code_4(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.cli.build import build_command

    monkeypatch.setattr("gobby.cli.build.resolve_project_id", lambda: "project-1")

    result = CliRunner().invoke(build_command, ["#42", "--profile", "quick"])

    assert result.exit_code == 4
    assert "Unknown build profile 'quick'" in result.output


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
    assert {"profile", "unattended", "isolation", "quick", "no_merge", "stage", "pr"}.issubset(
        schema["properties"]
    )
    assert {
        "stages",
        "add_stages",
        "yolo",
        "composer_yolo",
    }.isdisjoint(schema["properties"])
    assert {"profile", "unattended", "isolation", "quick", "no_merge", "stage", "pr"}.issubset(
        BuildRequest.model_fields
    )


class _ClosableDb:
    def close(self) -> None:
        pass
