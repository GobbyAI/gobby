"""Red tests for retry-cap CLI overrides."""

from __future__ import annotations

from typing import Any, cast

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit


def test_cli_overrides_propagate_to_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
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

    result = CliRunner().invoke(
        build_command,
        [
            "#42",
            "--max-expansion-attempts",
            "4",
            "--max-qa-rounds",
            "5",
            "--max-merge-attempts",
            "6",
            "--max-holistic-rounds",
            "7",
            "--max-review-rounds",
            "8",
        ],
    )

    assert result.exit_code == 0
    opts = cast(Any, captured["opts"])
    assert opts.max_expansion_attempts == 4
    assert opts.max_qa_rounds == 5
    assert opts.max_merge_attempts == 6
    assert opts.max_holistic_rounds == 7
    assert opts.max_review_rounds == 8


class _ClosableDb:
    def close(self) -> None:
        pass
