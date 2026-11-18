"""Landing policy path rules and freeze file reads."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from gobby.tasks.landing_policy import (
    UNREADABLE_FREEZE_REASON,
    LandingFreeze,
    classify_paths,
    freeze_path,
    read_freeze,
    write_freeze,
)

pytestmark = pytest.mark.unit

BOUND_TASK_ID = "7f1c2d3e-4b5a-4c6d-8e9f-0a1b2c3d4e5f"


def _write_freeze_file(git_common_dir: Path, payload: dict[str, object]) -> None:
    path = freeze_path(git_common_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        ([".gobby/workflows/agents/example.yaml", "src/gobby/example.py"], "restart"),
        ([".gobby/workflows/variables/example.yml", "web/src/example.ts"], "restart"),
        ([".gobby/workflows/agents/nested/example.yaml"], "none"),
        ([".gobby/workflows/variables/README.md"], "none"),
    ],
)
def test_project_workflow_activation_scope(paths: list[str], expected: str) -> None:
    assert classify_paths(iter(paths)) == expected


@pytest.mark.parametrize("kind", ["agents", "variables"])
@pytest.mark.parametrize("suffix", ["yaml", "yml"])
def test_project_agents_and_variables_require_restart(kind: str, suffix: str) -> None:
    path = f".gobby/workflows/{kind}/example.{suffix}"
    assert classify_paths(iter([path])) == "restart"


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["crates/gobby-core/src/lib.rs", "web/src/App.tsx"], "cutover"),
        (["src/gobby/storage/schema_expected_identity.json", "docs/x.md"], "cutover"),
        (["src/gobby/cli/main.py", "web/src/App.tsx"], "restart"),
        (["src/gobby/install/shared/workflows/agents/fixture-a.yaml"], "reload"),
        (["src/gobby/install/shared/workflows/agents/fixture-b.yaml", "docs/x.md"], "reload"),
        (["src/gobby/install/shared/workflows/agents/README.md"], "restart"),
        (["src/gobby/install/shared/workflows/agents/nested/seat.yaml"], "restart"),
        (
            [
                "src/gobby/install/shared/workflows/agents/fixture-a.yaml",
                "src/gobby/agents/sync.py",
            ],
            "restart",
        ),
        (["web/src/App.tsx"], "ui_build"),
        ([".gobby/workflows/pipelines/example-lane.yaml"], "restart"),
        ([".gobby/workflows/pipelines/example-lane.yml", "docs/x.md"], "restart"),
        ([".gobby/workflows/rules/operating-policy.yaml"], "restart"),
        ([".gobby/workflows/rules/operating-policy.yml", "docs/x.md"], "restart"),
        ([".gobby/workflows/rules/README.md"], "none"),
        ([".gobby/workflows/rules/nested/example.yaml"], "none"),
        ([".gobby/workflows/rules/operating-policy.yaml", "src/gobby/example.py"], "restart"),
        ([".gobby/workflows/rules/operating-policy.yaml", "crates/gcore/src/lib.rs"], "cutover"),
        ([".gobby/workflows/rules/operating-policy.yaml", "web/src/App.tsx"], "restart"),
        ([".gobby/workflows/pipelines/README.md"], "none"),
        ([".gobby/workflows/pipelines/nested/seat.yaml"], "none"),
        ([".gobby/workflows/pipelines/example-lane.yaml", "src/gobby/cli/main.py"], "restart"),
        ([".gobby/workflows/pipelines/example-lane.yaml", "crates/gcore/src/lib.rs"], "cutover"),
        ([".gobby/workflows/pipelines/example-lane.yaml", "web/src/App.tsx"], "restart"),
        (
            [
                ".gobby/workflows/pipelines/example-lane.yaml",
                "src/gobby/install/shared/workflows/agents/fixture-a.yaml",
            ],
            "restart",
        ),
        (
            ["src/gobby/install/shared/workflows/agents/fixture-a.yaml", "web/src/App.tsx"],
            "restart",
        ),
        (["docs/guides/x.md", "tests/test_x.py"], "none"),
        ([], "none"),
    ],
)
def test_classify_paths_strongest_class_wins(paths: list[str], expected: str) -> None:
    assert classify_paths(paths) == expected


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        '{"on": "yes", "reason": "", "set_by_session_id": null, "set_at": null}',
        '{"on": false, "reason": 3, "set_by_session_id": null, "set_at": null}',
    ],
)
def test_unreadable_freeze_file_reads_as_frozen(tmp_path: Path, content: str) -> None:
    assert read_freeze(tmp_path).on is False

    path = freeze_path(tmp_path)
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")
    freeze = read_freeze(tmp_path)

    assert freeze.on is True
    assert freeze.reason == UNREADABLE_FREEZE_REASON


def test_landing_freeze_task_id_is_the_last_field_and_defaults_to_none() -> None:
    fields = dataclasses.fields(LandingFreeze)
    freeze = LandingFreeze(on=False, reason="", set_by_session_id=None, set_at=None)

    assert fields[-1].name == "task_id"
    assert freeze.task_id is None


def test_write_freeze_binds_the_task_and_read_freeze_returns_it(tmp_path: Path) -> None:
    written = write_freeze(
        tmp_path, on=True, reason="release hold", session_id="holder", task_id=BOUND_TASK_ID
    )

    stored = read_freeze(tmp_path)

    assert written.task_id == BOUND_TASK_ID
    assert (stored.on, stored.reason, stored.set_by_session_id, stored.task_id) == (
        True,
        "release hold",
        "holder",
        BOUND_TASK_ID,
    )


def test_write_freeze_stores_the_bound_task_under_task_id_key(tmp_path: Path) -> None:
    write_freeze(
        tmp_path, on=True, reason="release hold", session_id="holder", task_id=BOUND_TASK_ID
    )

    stored = json.loads(freeze_path(tmp_path).read_text(encoding="utf-8"))

    assert stored["task_id"] == BOUND_TASK_ID


def test_write_freeze_without_a_task_reads_back_as_unbound(tmp_path: Path) -> None:
    write_freeze(tmp_path, on=True, reason="release hold", session_id="holder")

    freeze = read_freeze(tmp_path)

    assert (freeze.on, freeze.reason, freeze.task_id) == (True, "release hold", None)


@pytest.mark.parametrize("extra", [{}, {"task_id": None}])
def test_freeze_file_without_a_bound_task_reads_as_unbound(
    tmp_path: Path, extra: dict[str, object]
) -> None:
    _write_freeze_file(
        tmp_path,
        {"on": True, "reason": "release hold", "set_by_session_id": None, "set_at": None, **extra},
    )

    freeze = read_freeze(tmp_path)

    assert (freeze.on, freeze.reason, freeze.task_id) == (True, "release hold", None)


@pytest.mark.parametrize("task_id", [7, False, ["task"], {"id": "task"}])
def test_non_string_task_id_makes_the_freeze_unreadable(tmp_path: Path, task_id: object) -> None:
    _write_freeze_file(
        tmp_path,
        {
            "on": False,
            "reason": "release hold",
            "set_by_session_id": None,
            "set_at": None,
            "task_id": task_id,
        },
    )

    freeze = read_freeze(tmp_path)

    assert freeze.on is True
    assert freeze.reason == UNREADABLE_FREEZE_REASON
