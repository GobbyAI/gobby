"""Phase 2 red contracts for stage-state mutex serialization."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_mutex_serializes_writes": (
            "RuntimeDispatchMutex serializes every stage-state mutator against task_dispatch_mutex"
        ),
    },
    required_symbols=(
        "gobby.storage.tasks._dispatch_mutex:TaskDispatchMutexManager",
        "gobby.storage.tasks._stage_states:StageStatesManager",
    ),
)
