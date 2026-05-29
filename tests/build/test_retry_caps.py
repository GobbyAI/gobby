"""Red tests for retry-cap CLI overrides."""

from __future__ import annotations

from typing import Any, cast

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit


def test_cli_stage_cap_overrides_propagate_to_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr("gobby.cli.build.resolve_project_id", lambda _project_ref=None: "project-1")
    monkeypatch.setattr("gobby.cli.build._open_database", lambda: _ClosableDb())
    monkeypatch.setattr("gobby.cli.build._try_daemon_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("gobby.cli.build.build", fake_build)

    result = CliRunner().invoke(
        build_command,
        [
            "#42",
            "--stage",
            "expansion:max_work_attempts=4",
            "--stage",
            "development:max_review_rounds=5",
            "--stage",
            "merge:max_work_attempts=6",
            "--stage",
            "holistic_qa:max_review_rounds=7",
            "--stage",
            "pr:max_review_rounds=8",
        ],
    )

    assert result.exit_code == 0
    opts = cast(Any, captured["opts"])
    assert [
        (item.stage_name, item.max_work_attempts, item.max_review_rounds)
        for item in opts.stage_caps
    ] == [
        ("expansion", 4, None),
        ("development", None, 5),
        ("merge", 6, None),
        ("holistic_qa", None, 7),
        ("pr", None, 8),
    ]


class _ClosableDb:
    def close(self) -> None:
        pass
