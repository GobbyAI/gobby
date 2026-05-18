#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

CONSIDERED_EVENTS = frozenset({"push", "workflow_dispatch"})
GH_JSON_FIELDS = "createdAt,databaseId,status,conclusion,event,headSha,url"
DEFAULT_WORKFLOW = "CI"
DEFAULT_RUN_LIMIT = 50
DEFAULT_TIMEOUT_SECONDS = 7200.0
DEFAULT_POLL_INTERVAL_SECONDS = 30.0

GateState = Literal["pass", "fail", "wait"]
CommandRunner = Callable[[Sequence[str]], str]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]


class GateError(RuntimeError):
    """Raised when the CI gate cannot query or parse workflow runs."""


@dataclass(frozen=True)
class WorkflowRun:
    created_at: datetime
    database_id: int | None
    status: str
    conclusion: str | None
    event: str
    head_sha: str
    url: str

    @classmethod
    def from_gh_json(cls, raw: dict[str, object]) -> WorkflowRun:
        return cls(
            created_at=_parse_created_at(_required_str(raw, "createdAt")),
            database_id=_optional_int(raw, "databaseId"),
            status=_required_str(raw, "status").lower(),
            conclusion=_optional_str(raw, "conclusion"),
            event=_required_str(raw, "event"),
            head_sha=_required_str(raw, "headSha"),
            url=_required_str(raw, "url"),
        )

    @property
    def sort_key(self) -> tuple[datetime, int]:
        return (self.created_at, self.database_id or 0)

    def summary(self) -> str:
        conclusion = self.conclusion or "none"
        run_id = self.database_id if self.database_id is not None else "unknown"
        return (
            f"run {run_id}: event={self.event} status={self.status} "
            f"conclusion={conclusion} sha={self.head_sha} url={self.url}"
        )


@dataclass(frozen=True)
class GateDecision:
    state: GateState
    message: str
    run: WorkflowRun | None


def fetch_workflow_runs(
    *,
    repo: str,
    workflow: str,
    sha: str,
    runner: CommandRunner | None = None,
) -> list[WorkflowRun]:
    command_runner = runner or _run_gh
    command = [
        "gh",
        "run",
        "list",
        "--repo",
        repo,
        "--workflow",
        workflow,
        "--commit",
        sha,
        "--limit",
        str(DEFAULT_RUN_LIMIT),
        "--json",
        GH_JSON_FIELDS,
    ]
    output = command_runner(command)
    return parse_workflow_runs(output)


def parse_workflow_runs(output: str) -> list[WorkflowRun]:
    try:
        payload: object = json.loads(output)
    except json.JSONDecodeError as exc:
        raise GateError(f"gh returned invalid JSON: {exc}") from exc

    if not isinstance(payload, list):
        raise GateError("gh returned an unexpected payload; expected a list of workflow runs")

    runs: list[WorkflowRun] = []
    for item in payload:
        if not isinstance(item, dict):
            raise GateError("gh returned an unexpected workflow run; expected an object")
        runs.append(WorkflowRun.from_gh_json({str(key): value for key, value in item.items()}))
    return runs


def latest_considered_run(runs: Sequence[WorkflowRun], sha: str) -> WorkflowRun | None:
    considered = [run for run in runs if run.head_sha == sha and run.event in CONSIDERED_EVENTS]
    if not considered:
        return None
    return max(considered, key=lambda run: run.sort_key)


def evaluate_runs(runs: Sequence[WorkflowRun], sha: str) -> GateDecision:
    latest = latest_considered_run(runs, sha)
    if latest is None:
        return GateDecision(
            state="wait",
            message=(
                f"No {DEFAULT_WORKFLOW} run found for {sha} from considered events: "
                f"{', '.join(sorted(CONSIDERED_EVENTS))}"
            ),
            run=None,
        )

    if latest.status != "completed":
        return GateDecision(
            state="wait",
            message=f"Latest considered CI run is still {latest.status}: {latest.summary()}",
            run=latest,
        )

    if latest.conclusion == "success":
        return GateDecision(
            state="pass",
            message=f"Latest considered CI run passed: {latest.summary()}",
            run=latest,
        )

    conclusion = latest.conclusion or "none"
    return GateDecision(
        state="fail",
        message=f"Latest considered CI run did not pass ({conclusion}): {latest.summary()}",
        run=latest,
    )


def wait_for_ci_gate(
    *,
    repo: str,
    workflow: str,
    sha: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    runner: CommandRunner | None = None,
    sleep: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
) -> int:
    deadline = clock() + timeout_seconds
    while True:
        runs = fetch_workflow_runs(repo=repo, workflow=workflow, sha=sha, runner=runner)
        decision = evaluate_runs(runs, sha)

        if decision.state == "pass":
            print(decision.message)
            return 0
        if decision.state == "fail":
            print(f"::error::{decision.message}", file=sys.stderr)
            return 1

        now = clock()
        if now >= deadline:
            print(f"::error::Timed out waiting for CI gate: {decision.message}", file=sys.stderr)
            return 1

        print(decision.message)
        sleep(max(0.0, min(poll_interval_seconds, deadline - now)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Require the latest CI workflow run for a SHA to be successful."
    )
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--poll-interval-seconds", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS
    )
    args = parser.parse_args(argv)

    if not args.repo:
        parser.error("--repo is required when GITHUB_REPOSITORY is not set")
    if not args.sha:
        parser.error("--sha is required when GITHUB_SHA is not set")

    try:
        return wait_for_ci_gate(
            repo=args.repo,
            workflow=args.workflow,
            sha=args.sha,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
    except GateError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        print(f"::error::Failed to query GitHub Actions: {stderr}", file=sys.stderr)
        return 1


def _run_gh(command: Sequence[str]) -> str:
    completed = subprocess.run(
        list(command),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _parse_created_at(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _required_str(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise GateError(f"workflow run is missing required string field: {key}")
    return value


def _optional_str(raw: dict[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise GateError(f"workflow run field must be a string or null: {key}")
    return value.lower()


def _optional_int(raw: dict[str, object], key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise GateError(f"workflow run field must be an integer or null: {key}")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
