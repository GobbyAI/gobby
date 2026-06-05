from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gobby.memory.dream.cron import (
    MEMORY_DREAM_CRON_HANDLER,
    MEMORY_DREAM_CRON_JOB_NAME,
    register_memory_dream_cron,
)

pytestmark = pytest.mark.unit


def test_register_memory_dream_cron_creates_single_system_job() -> None:
    cron_storage = MagicMock()
    cron_storage.get_job_by_name.return_value = None
    cron_executor = MagicMock()
    config = SimpleNamespace(enabled=True, schedule_cron="0 3 * * *")

    registered = register_memory_dream_cron(
        cron_storage=cron_storage,
        cron_executor=cron_executor,
        memory_manager=MagicMock(),
        dream_config=config,
        project_id="proj-1",
    )

    assert registered == 1
    cron_executor.register_handler.assert_called_once()
    assert cron_executor.register_handler.call_args.args[0] == MEMORY_DREAM_CRON_HANDLER
    cron_storage.create_job.assert_called_once()
    kwargs = cron_storage.create_job.call_args.kwargs
    assert kwargs["name"] == MEMORY_DREAM_CRON_JOB_NAME
    assert kwargs["schedule_type"] == "cron"
    assert kwargs["cron_expr"] == "0 3 * * *"
    assert kwargs["action_config"] == {"handler": MEMORY_DREAM_CRON_HANDLER}
    assert kwargs["is_system"] is True


def test_register_memory_dream_cron_does_not_register_pipeline_action() -> None:
    cron_storage = MagicMock()
    cron_storage.get_job_by_name.return_value = None
    cron_executor = MagicMock()

    register_memory_dream_cron(
        cron_storage=cron_storage,
        cron_executor=cron_executor,
        memory_manager=MagicMock(),
        dream_config=SimpleNamespace(enabled=True, schedule_cron="0 3 * * *"),
        project_id="proj-1",
    )

    kwargs = cron_storage.create_job.call_args.kwargs
    assert kwargs["action_type"] == "handler"
    assert kwargs["action_config"] == {"handler": MEMORY_DREAM_CRON_HANDLER}
