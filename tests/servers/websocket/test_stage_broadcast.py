"""Phase 2 red contracts for stage websocket broadcasts."""

from __future__ import annotations

import pytest

from tests.phase2_stage_contract_helpers import register_contract_tests

pytestmark = pytest.mark.unit

register_contract_tests(
    globals(),
    {
        "test_stage_transition_broadcasts": (
            "stage transitions emit websocket broadcasts with task_id, stage_name, and state"
        ),
    },
    required_symbols=("gobby.servers.websocket.broadcast:StageTransitionEvent",),
)
