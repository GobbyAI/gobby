"""A background Bash receipt names the output file that settles the call's claimed edits (#25161)."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.sessions.background_notices import background_receipt

RECEIPT = (
    "Command running in background with ID: bq76hb30g. "
    "Output is being written to: /scratch/tasks/bq76hb30g.output. "
    "You will be notified when it completes."
)
TIMED_OUT_RECEIPT = (
    "Command did not complete within its 120s timeout and was moved to the background "
    "(ID: bq76hb30g). Output is being written to: /scratch/tasks/bq76hb30g.output. "
)


@pytest.mark.parametrize(
    "data",
    [
        {"tool_response": {"backgroundTaskId": "bq76hb30g", "stdout": RECEIPT}},
        {"tool_output": RECEIPT},
    ],
)
def test_background_receipt_yields_its_output_path(data: dict[str, Any]) -> None:
    receipt = background_receipt(data)

    assert receipt is not None
    assert receipt.output_path == "/scratch/tasks/bq76hb30g.output"


def test_foreground_result_yields_no_receipt() -> None:
    assert background_receipt({"tool_response": {"stdout": "hello"}}) is None


@pytest.mark.parametrize(
    "data",
    [
        {
            "tool_response": {
                "stdout": f"tests/hooks/test_background_shell_receipts.py:13:    {RECEIPT}"
            }
        },
        {"tool_output": f"tests/hooks/test_background_shell_receipts.py:13:    {RECEIPT}"},
    ],
)
def test_a_foreground_result_that_prints_a_receipt_yields_no_receipt(data: dict[str, Any]) -> None:
    # A grep of this file prints the receipt sentence after a path prefix; the job never ran.
    assert background_receipt(data) is None


@pytest.mark.parametrize("text", [RECEIPT, TIMED_OUT_RECEIPT])
def test_each_background_receipt_shape_yields_its_output_path_and_job(text: str) -> None:
    # A command that outlives its timeout is moved to the background with the same output file.
    receipt = background_receipt({"tool_output": text})

    assert receipt is not None
    assert receipt.job_id == "bq76hb30g"
    assert receipt.output_path == "/scratch/tasks/bq76hb30g.output"
