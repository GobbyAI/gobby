"""The bundled Test Writer leaves an epic root unclaimed and waits for a leaf to claim."""

from __future__ import annotations

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from tests.agents._yaml_helpers import flat

pytestmark = pytest.mark.unit


def _start_prompt() -> str:
    path = next(get_bundled_agents_path().rglob("test-writer.yaml"))
    definition = yaml.safe_load(path.read_text(encoding="utf-8"))
    return flat(definition["prompts"]["agent"])


def test_test_writer_does_not_claim_an_epic_root_at_start() -> None:
    start = _start_prompt()

    assert "Then claim the task with" not in start
    assert "`task_type` `epic`" in start
    assert "never claimed by a seat" in start


def test_test_writer_claims_only_the_leaf_its_dev_manager_names() -> None:
    start = _start_prompt()

    assert "wait with `gobby-agents:wait_for_coordination` on your Dev Manager" in start
    assert "then claim only that leaf" in start
