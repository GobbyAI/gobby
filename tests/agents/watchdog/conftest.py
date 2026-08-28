"""Fixtures shared by the watchdog tests, re-exported from the lifecycle monitor suite."""

from __future__ import annotations

from tests.agents.test_lifecycle_monitor import (
    _local_machine_identity,
    agent_run_manager,
    sample_session,
)

__all__ = ["_local_machine_identity", "agent_run_manager", "sample_session"]
