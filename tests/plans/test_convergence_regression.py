from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.plans.convergence_regression import (
    PLAN_COMMAND,
    assert_convergence_targets,
    assert_wall_time_variance,
    build_convergence_comparison,
    run_live_convergence_regression,
)
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.sessions.analyzer import HandoffContext
from gobby.sessions.summary_context import _build_summary_prompt_context
from gobby.sessions.workspace_context import enrich_git_context
from tests.review_telemetry_helpers import delivered_telemetry

_PLAN_PATH = Path(".gobby/plans/completed/context-mode-borrowings.md")
_OTHER_PLAN_PATH = Path(".gobby/plans/adversary-convergence-improvements.md")
_CANONICAL_LANES = ("requirements", "failure-paths", "integration")


def _classification(check_key: str, check_key_class: str) -> dict[str, object]:
    return {
        "check_key": check_key,
        "check_key_class": check_key_class,
        "finding_ids": [f"finding-{check_key}"],
        "ledger_ids": [],
        "classification_inputs": [{"name": "persisted", "value": "true"}],
    }


def _telemetry(
    *,
    check_key: str,
    check_key_class: str,
    wall_time_seconds: float,
    fixer_induced: bool = False,
    ledger_entries: int = 0,
) -> dict[str, object]:
    telemetry = cast(dict[str, Any], deepcopy(delivered_telemetry()))
    reviewer = cast(dict[str, Any], telemetry["reviewer"])
    classification = _classification(check_key, check_key_class)
    reviewer["reviewer_miss"] = {
        "count": 1,
        "classifications": [classification],
    }
    reviewer["fixer_induced"] = {
        "count": int(fixer_induced),
        "classifications": [classification] if fixer_induced else [],
    }
    reviewer["repeated_check_keys"] = {"count": 0, "classifications": []}
    ledger_ids = [f"ledger-{index}" for index in range(ledger_entries)]
    reviewer["ledger_entries_carried"] = {
        "count": ledger_entries,
        "finding_ids": [],
        "ledger_ids": ledger_ids,
        "classification_inputs": [{"name": "persisted", "value": "true"}],
    }
    telemetry["state"] = "enriched"
    telemetry["daemon"] = {
        "terminal_status": "success",
        "wall_time_seconds": wall_time_seconds,
        "tool_calls": 12,
        "turns": 3,
        "calls_per_finding": {"value": 3.0},
        "lanes": [
            {
                "lane_id": lane_id,
                "duration_seconds": {"value": 1.0},
                "tool_calls": {"value": 4.0},
            }
            for lane_id in _CANONICAL_LANES
        ],
    }
    return telemetry


def _persisted_rounds() -> list[dict[str, object]]:
    finding_counts = (5, 3, 1, 0)
    classes = ("requirements-shape", "failure-path", "integration", "approval")
    return [
        {
            "round_number": round_number,
            "verdict": "approved" if finding_count == 0 else "needs_review",
            "findings": [
                {"finding_id": f"round-{round_number}-finding-{index}"}
                for index in range(finding_count)
            ],
            "convergence_telemetry": _telemetry(
                check_key=f"round-{round_number}-check",
                check_key_class=classes[round_number - 1],
                wall_time_seconds=(12.0, 11.0, 10.0, 9.0)[round_number - 1],
                fixer_induced=round_number == 2,
                ledger_entries=round_number,
            ),
        }
        for round_number, finding_count in enumerate(finding_counts, start=1)
    ]


def test_live_regression_writes_comparison_artifact(tmp_path: Path) -> None:
    persisted_path = tmp_path / "persisted-rounds.json"
    artifact_path = tmp_path / "context-mode-borrowings-comparison.json"
    invocations: list[tuple[str, Path]] = []

    def run_plan(command: str, plan_path: Path) -> None:
        invocations.append((command, plan_path))
        persisted_path.write_text(json.dumps(_persisted_rounds()), encoding="utf-8")

    def load_rounds(_plan_path: Path) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], json.loads(persisted_path.read_text()))

    artifact = run_live_convergence_regression(
        plan_path=_PLAN_PATH,
        artifact_path=artifact_path,
        baseline_rounds_to_approval=24,
        run_plan=run_plan,
        load_persisted_rounds=load_rounds,
    )

    assert invocations == [(PLAN_COMMAND, _PLAN_PATH)]
    assert json.loads(artifact_path.read_text()) == artifact
    assert artifact == {
        "command": "/gobby plan",
        "plan_path": ".gobby/plans/completed/context-mode-borrowings.md",
        "baseline": {"rounds_to_approval": 24},
        "current": {
            "rounds_to_approval": 4,
            "fixer_induced_count": 1,
            "repeated_check_keys": [],
            "check_key_classes_by_round": [
                ["requirements-shape"],
                ["failure-path"],
                ["integration"],
                ["approval"],
            ],
            "per_round_wall_time_seconds": [12.0, 11.0, 10.0, 9.0],
            "ledger_entries_carried": 10,
            "finding_tail": [5, 3, 1, 0],
            "lane_ids_by_round": [list(_CANONICAL_LANES)] * 4,
        },
    }


def test_convergence_targets_asserted() -> None:
    artifact = build_convergence_comparison(
        plan_path=_PLAN_PATH,
        baseline_rounds_to_approval=24,
        persisted_rounds=_persisted_rounds(),
    )
    assert_convergence_targets(artifact)

    regressions = {
        "rounds-to-approval": ("rounds_to_approval", 10),
        "exact check-key repeat": ("repeated_check_keys", ["same-check"]),
        "non-decaying finding tail": ("finding_tail", [5, 3, 3, 0]),
    }
    for message, (field, value) in regressions.items():
        regressed = deepcopy(artifact)
        cast(dict[str, object], regressed["current"])[field] = value
        with pytest.raises(AssertionError, match=message):
            assert_convergence_targets(regressed)

    class_regression = deepcopy(artifact)
    cast(dict[str, object], class_regression["current"])["check_key_classes_by_round"] = [
        ["requirements-shape"],
        ["shared-class"],
        ["shared-class"],
        ["approval"],
    ]
    with pytest.raises(AssertionError, match="consecutive rounds"):
        assert_convergence_targets(class_regression)

    missing_lane = _persisted_rounds()
    daemon = cast(dict[str, Any], missing_lane[0]["convergence_telemetry"])["daemon"]
    cast(dict[str, Any], daemon)["lanes"] = cast(dict[str, Any], daemon)["lanes"][:-1]
    with pytest.raises(ReviewEvidenceError, match="canonical lane"):
        build_convergence_comparison(
            plan_path=_PLAN_PATH,
            baseline_rounds_to_approval=24,
            persisted_rounds=missing_lane,
        )

    slow_run = deepcopy(artifact)
    cast(dict[str, object], slow_run["current"])["per_round_wall_time_seconds"] = [
        12.0,
        11.0,
        10.0,
        120.0,
    ]
    assert_convergence_targets(slow_run)
    with pytest.raises(AssertionError, match="wall-time variance"):
        assert_wall_time_variance(slow_run, maximum_ratio=3.0)


class _GitProcess:
    def __init__(self, stdout: str) -> None:
        self.returncode = 0
        self._stdout = stdout

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout.encode(), b""


@pytest.mark.asyncio
async def test_compaction_isolation_under_concurrent_editors() -> None:
    own_path = str(_PLAN_PATH)
    other_path = str(_OTHER_PLAN_PATH)
    handoff_ctx = HandoffContext(files_modified=[own_path])

    async def create_subprocess_exec(*args: object, **_kwargs: object) -> _GitProcess:
        scoped = "--" in args and own_path in args
        if "status" in args:
            output = f" M {own_path}" if scoped else f" M {own_path}\n M {other_path}"
        else:
            output = (
                f"own-hash|update {own_path}"
                if scoped
                else f"own-hash|update {own_path}\nother-hash|update {other_path}"
            )
        return _GitProcess(output)

    with patch(
        "gobby.sessions.workspace_context.asyncio.create_subprocess_exec",
        side_effect=create_subprocess_exec,
    ):
        await enrich_git_context(handoff_ctx, Path.cwd())

    def run_git(
        args: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        scoped = args[-2:] == ["--", own_path]
        paths = [own_path] if scoped else [own_path, other_path]
        if "--name-status" in args:
            stdout = "\n".join(f"M\t{path}" for path in paths)
        elif "ls-files" in args:
            stdout = ""
        elif "--stat" in args:
            stdout = "\n".join(f"{path} | 1 +" for path in paths)
        else:
            stdout = "\n".join(f"diff --git a/{path} b/{path}" for path in paths)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    session = MagicMock()
    session.id = "reviewer-session"
    session.source = "codex"
    session.summary_markdown = "### Turn 1\nReviewed the target plan."
    manager = MagicMock()
    manager.db = None
    with patch("gobby.workflows.git_utils.subprocess.run", side_effect=run_git):
        context = await _build_summary_prompt_context(
            session=session,
            turns=[],
            handoff_ctx=handoff_ctx,
            db=None,
            session_manager=manager,
            project_path=str(Path.cwd()),
        )

    rendered = json.dumps(context, sort_keys=True)
    assert own_path in rendered
    assert other_path not in rendered
