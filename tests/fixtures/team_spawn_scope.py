"""Synthetic team definitions for spawn-admission tests."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import make_team_world, running_seat


def _define_agent(
    db: HubDatabase,
    name: str,
    spawnable: list[str] | None,
    fallback: str | None = None,
    *,
    variables: dict[str, Any] | None = None,
    capabilities: tuple[str, ...] = (),
    resumable: tuple[str, ...] = (),
) -> None:
    body: dict[str, Any] = {
        "name": name,
        "prompts": {"agent": "Work."},
        "workflows": {"rule_selectors": {"include": []}, "variables": variables or {}},
        "capabilities": list(capabilities),
        "resumable_team_agents": list(resumable),
    }
    if spawnable is not None:
        body["spawnable_agents"] = spawnable
    if fallback is not None:
        body["fallback_agent"] = fallback
    AgentDefinitionManager(db).create(name, body)


TEAM_SEAT_TYPES = (
    "fixture-definition-c02389e4",
    "fixture-worker",
    "fixture-definition-e08ba693",
    "custom-maker",
)


@dataclass
class _Teams:
    db: HubDatabase
    manager: Session
    seats: dict[str, Session]
    outsider: Session

    def target(self, name: str) -> Session:
        return self.outsider if name == "outsider" else self.seats[name]


@pytest.fixture
async def teams(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _Teams:
    world = make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )
    runbooks = []
    for title in ("Resume a seat of this team", "Another team's task"):
        task = world.case.task(title)
        runbook = world.child(task)
        await world.admit(runbook, task)
        runbooks.append(runbook)
    own, other = runbooks
    built = _Teams(
        db=temp_db,
        manager=running_seat(world, own, "fixture-definition-f0ad0fd9"),
        seats={
            name: running_seat(world, own, name)
            for name in (*TEAM_SEAT_TYPES, "fixture-coordinator")
        },
        outsider=running_seat(world, other, "fixture-worker"),
    )
    for runbook in runbooks:
        world.finish(runbook)
    return built
