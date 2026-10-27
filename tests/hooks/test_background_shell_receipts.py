"""A background Bash receipt names the output file that settles the call's claimed edits (#25161)."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.hooks.event_handlers._tool import _background_output_path

RECEIPT = (
    "Command running in background with ID: bq76hb30g. "
    "Output is being written to: /scratch/tasks/bq76hb30g.output. "
    "You will be notified when it completes."
)


@pytest.mark.parametrize(
    "data",
    [
        {"tool_response": {"backgroundTaskId": "bq76hb30g", "stdout": RECEIPT}},
        {"tool_output": RECEIPT},
    ],
)
def test_background_receipt_yields_its_output_path(data: dict[str, Any]) -> None:
    assert _background_output_path(data) == "/scratch/tasks/bq76hb30g.output"


def test_foreground_result_yields_no_output_path() -> None:
    assert _background_output_path({"tool_response": {"stdout": "hello"}}) is None
