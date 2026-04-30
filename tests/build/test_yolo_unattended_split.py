"""Red tests for the unattended/composer-yolo build split."""

from typing import Any, cast

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit


def test_build_options_carries_unattended_and_composer_yolo() -> None:
    from gobby.build.service import BuildOptions

    fields = set(getattr(BuildOptions, "__dataclass_fields__", {}))
    assert {"unattended", "composer_yolo"}.issubset(fields)
    assert "yolo" not in fields

    opts = BuildOptions(
        profile="auto",
        skip_stages=[],
        isolation="worktree",
        unattended=True,
        composer_yolo=False,
        max_review_rounds=3,
    )
    assert opts.unattended is True
    assert opts.composer_yolo is False


def test_yolo_flag_is_noop_with_composer_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.cli.build import build_command

    captured: dict[str, object] = {}

    async def fake_build(input_ref: str, opts: object, **kwargs: object) -> object:
        from gobby.build.service import BuildResult

        captured["opts"] = opts
        return BuildResult(
            task_id="task-1",
            created=False,
            initial_lifecycle="in_development",
            applied_stages_skipped=[],
            tick_dispatched=0,
        )

    monkeypatch.setattr("gobby.cli.build.resolve_project_id", lambda: "project-1")
    monkeypatch.setattr("gobby.cli.build.LocalDatabase", lambda: _ClosableDb())
    monkeypatch.setattr("gobby.cli.build.build", fake_build)

    result = CliRunner().invoke(build_command, ["#42", "--yolo"])

    assert result.exit_code == 0
    opts = cast(Any, captured["opts"])
    assert opts.composer_yolo is True
    assert opts.unattended is False


def test_flag_propagation_distinct_fields_on_json_surfaces(temp_db) -> None:
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
    schema = tool["inputSchema"]
    assert schema["properties"]["unattended"]["type"] == "boolean"
    assert schema["properties"]["composer_yolo"]["type"] == "boolean"
    assert "unattended" in BuildRequest.model_fields
    assert "composer_yolo" in BuildRequest.model_fields


def test_composer_cap_rejects_upstream_for_plan_epic_leaf() -> None:
    from gobby.build.service import _composer_scope_cap

    for input_kind in ("plan_file", "epic", "leaf"):
        with pytest.raises(ValueError, match="composer.*upstream"):
            _composer_scope_cap(input_kind=input_kind, composer_authority="upstream")


def test_composer_cap_allows_upstream_for_ideate() -> None:
    from gobby.build.service import _composer_scope_cap

    assert _composer_scope_cap(input_kind="ideate", composer_authority="upstream") == "upstream"


class _ClosableDb:
    def close(self) -> None:
        pass
