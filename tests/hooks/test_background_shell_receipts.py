"""A background Bash receipt names the output file that settles the call's claimed edits (#25161)."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.sessions.background_notices import BackgroundReceipt, background_receipt

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
def test_a_result_that_prints_a_receipt_line_is_a_background_call(data: dict[str, Any]) -> None:
    # A grep of this file prints the receipt sentence after a path prefix. A receipt-shaped line
    # anywhere in unstructured stdout is a background call, so it never settles on the foreground
    # path. It stays in flight: no notice can carry this call's tool-use id.
    receipt = background_receipt(data)

    assert receipt == BackgroundReceipt(
        job_id="bq76hb30g", output_path="/scratch/tasks/bq76hb30g.output"
    )


PROBE_STDOUT = (
    "build output\n"
    "Command running in background with ID: job123. "
    "Output is being written to: /tmp/job123.output."
)


@pytest.mark.parametrize(
    "data",
    [
        {"tool_response": {"stdout": PROBE_STDOUT}},
        {"tool_response": {"backgroundTaskId": "job123", "stdout": PROBE_STDOUT}},
    ],
    ids=["unstructured", "structured"],
)
def test_a_receipt_after_other_stdout_names_its_job_and_file(data: dict[str, Any]) -> None:
    # The receipt is not the first line. Unstructured, it is still a background call, identified.
    assert background_receipt(data) == BackgroundReceipt(
        job_id="job123", output_path="/tmp/job123.output"
    )


def test_an_unstructured_receipt_that_names_no_job_or_file_stays_in_flight() -> None:
    # The sentence names neither job nor file: the call is background, and stays unidentified.
    data = {"tool_response": {"stdout": "build output\nCommand running in background with ID: ."}}

    assert background_receipt(data) == BackgroundReceipt(job_id=None, output_path=None)


@pytest.mark.parametrize("text", [RECEIPT, TIMED_OUT_RECEIPT])
def test_each_background_receipt_shape_yields_its_output_path_and_job(text: str) -> None:
    # A command that outlives its timeout is moved to the background with the same output file.
    receipt = background_receipt({"tool_output": text})

    assert receipt is not None
    assert receipt.job_id == "bq76hb30g"
    assert receipt.output_path == "/scratch/tasks/bq76hb30g.output"


PRINTED_BEFORE_RECEIPT = (
    "     680\n"
    "Command did not complete within its 120s timeout and was moved to the background "
    "(ID: bev9oynfh). Output is being written to: /scratch/tasks/bev9oynfh.output. "
    "You will be notified when it completes."
)


def test_a_background_result_names_its_receipt_after_other_stdout() -> None:
    # The harness backgrounded a compound command whose own stdout printed before the receipt.
    data = {"tool_response": {"backgroundTaskId": "bev9oynfh", "stdout": PRINTED_BEFORE_RECEIPT}}

    receipt = background_receipt(data)

    assert receipt is not None
    assert receipt.job_id == "bev9oynfh"
    assert receipt.output_path == "/scratch/tasks/bev9oynfh.output"


def test_a_background_result_without_its_receipt_text_stays_unidentified() -> None:
    # The structured result names the job, but its receipt sentence is not in the stdout. The
    # call is still background: it keeps its paths in flight, with no output file to settle on.
    data = {
        "tool_response": {
            "backgroundTaskId": "bev9oynfh",
            "stdout": "     680",
            "interrupted": False,
        }
    }

    receipt = background_receipt(data)

    assert receipt is not None
    assert receipt.job_id == "bev9oynfh"
    assert receipt.output_path is None
