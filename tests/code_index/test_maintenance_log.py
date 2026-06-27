"""Tests for code-index maintenance log helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.code_index import maintenance_log

pytestmark = pytest.mark.unit


def test_logger_memoizes_fallback_logger_after_handler_setup_failure(tmp_path: Path) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("file", encoding="utf-8")
    log_file = blocked_parent / "maintenance.log"
    expanded = str(log_file.expanduser())
    maintenance_log._LOGGERS.pop(expanded, None)

    try:
        with patch.object(maintenance_log._FALLBACK_LOGGER, "warning") as warning:
            first = maintenance_log._logger(str(log_file))
            second = maintenance_log._logger(str(log_file))

        assert first is maintenance_log._FALLBACK_LOGGER
        assert second is maintenance_log._FALLBACK_LOGGER
        warning.assert_called_once()
    finally:
        maintenance_log._LOGGERS.pop(expanded, None)
