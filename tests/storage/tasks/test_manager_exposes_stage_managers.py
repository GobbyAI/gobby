"""LocalTaskManager stage-manager composition contracts."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager
from tests.storage.tasks._stage_test_helpers import (
    require_stage_registry_types,
    require_stage_state_types,
)

pytestmark = pytest.mark.unit


def test_managers_accessible(temp_db) -> None:
    _, StageRegistryManager = require_stage_registry_types()
    types = require_stage_state_types()
    StageStatesManager = types["StageStatesManager"]

    manager = LocalTaskManager(temp_db)

    assert isinstance(manager.stages_registry, StageRegistryManager)
    assert isinstance(manager.stage_states, StageStatesManager)
    assert manager.stages_registry is manager.stages_registry
    assert manager.stage_states is manager.stage_states
    assert manager.stage_states is not manager.stages_registry
