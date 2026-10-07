"""Orphan projection failures retain their diagnostic in the maintenance log."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.code_index import maintenance_log
from gobby.code_index.gcode_gateway import GcodeCommandError
from gobby.code_index.maintenance import _sweep_orphaned_index_projects
from gobby.config.logging import LoggingSettings
from gobby.telemetry.logging import setup_file_logging
from tests.code_index.test_code_index_maintenance import (
    RecordingGcodeGateway,
    _orphan_storage,
    _orphan_sweep_context,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("step", ["graph_clear", "vector_clear"])
async def test_orphan_failure_reaches_configured_maintenance_log(tmp_path: Path, step: str) -> None:
    project_id = "63dac488-3956-5023-a761-6d0ad76c2601"
    log_file = tmp_path / "code-index-maintenance.log"
    setup_file_logging(LoggingSettings(dir=str(tmp_path), level="debug", format="text"))
    storage = _orphan_storage([project_id])
    gateway = RecordingGcodeGateway()
    context = _orphan_sweep_context(storage, gateway)
    context.config.maintenance_log_file = str(log_file)
    error = GcodeCommandError(
        ["gcode", "graph", "clear", f"--project-id={project_id}"],
        2,
        '{"error":"malformed","message":'
        '"malformed grant: grant project does not match local project"}',
    )
    configured_logger = maintenance_log._logger(str(log_file))
    try:
        with patch.object(gateway, step, new=AsyncMock(side_effect=error)):
            assert await _sweep_orphaned_index_projects(context) == 0
        storage.purge_index_project.assert_not_called()
        contents = log_file.read_text()
        assert project_id in contents
        assert "GcodeCommandError" in contents
        assert "grant project does not match local project" in contents
        assert "Traceback" in contents
        event = json.loads(contents)
        assert event["exception_type"] == "GcodeCommandError"
        assert event["status"] == "failed"
        central_errors = (tmp_path / "errors.log").read_text()
        assert "GcodeCommandError" in central_errors
        assert "grant project does not match local project" in central_errors
    finally:
        for handler in tuple(configured_logger.handlers):
            configured_logger.removeHandler(handler)
            handler.close()
        maintenance_log._LOGGERS.pop(str(log_file), None)
