"""Escalation readers must use Task.is_escalated directly."""

from __future__ import annotations

import pytest

from tests.phase5_contract_helpers import source_texts

pytestmark = pytest.mark.unit


def test_no_helper_calls() -> None:
    scoped = source_texts(
        (
            "src/gobby/dispatch",
            "src/gobby/tasks/state_semantics.py",
            "src/gobby/servers/routes/tasks.py",
        )
    )

    assert "is_task_escalated(" not in scoped
    assert "get_pre_escalation_status(" not in scoped
    assert "status == 'escalated'" not in scoped
    assert 'status == "escalated"' not in scoped
