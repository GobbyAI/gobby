"""Contracts for the credential-shape scan over the communications and voice corpus."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.contracts.comms.credential_scan import find_credential_shapes, scan_corpus

CASE_FILE_FLOOR = 41
EDGE_SCRIPT_FLOOR = 5
WIRE_DIR = Path(__file__).resolve().parent / "adapter_wire"
# Five providers, each with a send, an inbound and an attachment fixture.
WIRE_FIXTURE_COUNT = 15


@pytest.mark.unit
def test_corpus_holds_no_credential_shaped_values() -> None:
    report = scan_corpus()
    assert report.case_files >= CASE_FILE_FLOOR, (
        f"the scan covered {report.case_files} case files, expected at least {CASE_FILE_FLOOR}"
    )
    assert report.edge_files >= EDGE_SCRIPT_FLOOR, (
        f"the scan covered {report.edge_files} edge scripts, expected at least {EDGE_SCRIPT_FLOOR}"
    )
    assert report.findings == [], f"the scan flagged: {report.findings}"


@pytest.mark.unit
def test_scan_covers_every_adapter_wire_fixture() -> None:
    fixtures = sorted(WIRE_DIR.glob("*.json"))
    assert len(fixtures) == WIRE_FIXTURE_COUNT, (
        f"{WIRE_DIR} holds {len(fixtures)} fixtures, expected {WIRE_FIXTURE_COUNT}"
    )
    report = scan_corpus()
    assert report.wire_files == len(fixtures), (
        f"the scan covered {report.wire_files} adapter wire fixtures of {len(fixtures)}"
    )
    assert report.findings == [], f"the scan flagged: {report.findings}"


@pytest.mark.unit
def test_scanner_flags_a_planted_slack_token() -> None:
    # Assembled at run time, so this file holds no token-shaped literal of its own.
    slack_token = "xox" + "b-" + "1234567890-1234567890123-" + "a" * 24
    telegram_token = "1234567890:" + "A" * 35
    text = f'{{"bot_token": "{slack_token}"}}\n{{"token": "{telegram_token}"}}\n'
    flagged = find_credential_shapes(text)
    assert sorted(flagged) == ["slack_bot_token", "telegram_bot_token"], (
        f"the scanner flagged {flagged}"
    )
