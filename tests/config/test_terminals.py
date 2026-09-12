"""Terminal host configuration acceptance tests."""

import pytest
from pydantic import ValidationError

from gobby.config.terminal_host import TerminalHostConfig

pytestmark = pytest.mark.unit


def test_host_restart_fields_validate() -> None:
    config = TerminalHostConfig()
    assert config.restart_max_attempts == 5
    assert config.restart_backoff_ceiling_seconds == 30.0

    with pytest.raises(ValidationError):
        TerminalHostConfig(restart_max_attempts=0)
    with pytest.raises(ValidationError):
        TerminalHostConfig(restart_backoff_ceiling_seconds=0)
