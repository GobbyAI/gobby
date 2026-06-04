from __future__ import annotations

import pytest

from gobby.cli._build_output import _echo_target_control_result

pytestmark = pytest.mark.unit


def test_echo_target_control_result_handles_missing_root_task_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _echo_target_control_result({"action": "pause"})

    assert "Root task: <unknown>" in capsys.readouterr().out
