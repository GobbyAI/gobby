"""Shared protocol-line grammar for the seat envelopes that transitions consume."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from gobby.storage.inter_session_messages import InterSessionMessage
from gobby.workflows.engine.seat_messages import (
    _COORDINATION_GRAMMAR,
    CandidateMessageError,
    candidate_event,
)

pytestmark = pytest.mark.unit

SHA = "0123456789abcdef0123456789abcdef01234567"


def _parse(text: str) -> dict[str, str] | None:
    from gobby.workflows.engine import seat_messages

    return seat_messages.envelope_fields(text)


@pytest.mark.parametrize(
    "line",
    [
        f"EVENT=CANDIDATE TASK=#7 TASK_TITLE=Seat proofs RUN=run WT=tree COMMIT={SHA} NOTE=ready",
        f"LANE=2 EVENT=CANDIDATE TASK=#7 RUN=run WT=tree COMMIT={SHA} NOTE=ready",
        f"LANE= EVENT=CANDIDATE TASK=#7 RUN=run WT=tree COMMIT={SHA} NOTE=ready",
        f"EVENT=CANDIDATE TASK=#7 WT=tree COMMIT={SHA} RUN=run NOTE=ready",
        f"EVENT=CANDIDATE TASK=#7 RUN=run WT=tree COMMIT={SHA.upper()}",
        f"EVENT=CANDIDATE_WITHDRAWN TASK=#7 SHA={SHA} NOTE=reassigned",
        f"EVENT=CANDIDATE_VERDICT TASK=#7 SHA={SHA} VERDICT=BOUNCE TASK_TITLE=Seat NOTE=HIGH",
    ],
)
def test_consumed_well_formed_envelopes_parse(line: str) -> None:
    fields = _parse(line)

    assert fields is not None
    assert fields["TASK"] == "#7"
    assert fields["EVENT"] == line.split("EVENT=", 1)[1].split(" ", 1)[0]


def test_candidate_sha_is_normalized_to_lowercase() -> None:
    fields = _parse(f"EVENT=CANDIDATE TASK=#7 RUN=run WT=tree COMMIT={SHA.upper()}")

    assert fields is not None
    assert fields["COMMIT"] == SHA


def test_candidate_run_with_whitespace_names_run() -> None:
    with pytest.raises(CandidateMessageError) as caught:
        _parse(f"EVENT=CANDIDATE TASK=#7 RUN=green pass WT=tree COMMIT={SHA}")

    assert caught.value.field == "RUN"
    assert caught.value.canonical == "TASK_TITLE RUN WT COMMIT|DISPOSITION NOTE"
    assert "TASK_TITLE RUN WT COMMIT|DISPOSITION NOTE" in str(caught.value)


@pytest.mark.parametrize(
    ("line", "field"),
    [
        (f"EVENT=CANDIDATE TASK=#7 RUN=run WT=tree COMMIT={SHA[:10]}", "COMMIT"),
        (f"EVENT=CANDIDATE TASK=#7 RUN=run WT=tree COMMIT={SHA} RUN=other", "RUN"),
        (f"EVENT=CANDIDATE TASK=#7 WT=tree COMMIT={SHA}", "RUN"),
        (f"EVENT=CANDIDATE RUN=run WT=tree COMMIT={SHA}", "TASK"),
        (f"EVENT=CANDIDATE TASK=#7 RUN=run WT=tree COMMIT={SHA} TASK=#8", "TASK"),
        (f"EVENT=CANDIDATE_VERDICT TASK=#7 SHA={SHA} TASK_TITLE=t VERDICT=BOUNCE", "VERDICT"),
        (f"EVENT=CANDIDATE_WITHDRAWN TASK=#7 SHA={SHA} BOGUS=1", "BOGUS"),
        (f"EVENT=CANDIDATE_VERDICT TASK=#7 SHA={SHA} VERDICT=BOUNCE SHA={SHA}", "SHA"),
        ("EVENT=PROOF_REQUEST TASK=#7 NOTE=prove it now", "SHA"),
        (f"EVENT=PROOF_REQUEST TASK=#7 SHA={SHA}ab NOTE=prove it now", "SHA"),
        (f"EVENT=PROOF_REQUEST TASK=#7 SHA={SHA} BOGUS=1", "BOGUS"),
    ],
)
def test_malformed_consumed_envelopes_name_the_offending_field(line: str, field: str) -> None:
    with pytest.raises(CandidateMessageError) as caught:
        _parse(line)

    assert caught.value.field == field
    assert caught.value.canonical is not None
    assert caught.value.canonical in str(caught.value)


def test_candidate_metadata_keys_are_allowed() -> None:
    fields = _parse(
        f"EVENT=CANDIDATE TASK=#7 TASK_TITLE=Seat proofs RUN=run WT=tree COMMIT={SHA} "
        "BASE=base TESTS=481 NOTE=481 passed"
    )

    assert fields is not None
    assert fields["TESTS"] == "481"


def test_candidate_stray_word_after_metadata_names_that_key() -> None:
    with pytest.raises(CandidateMessageError) as caught:
        _parse(f"EVENT=CANDIDATE TASK=#7 RUN=run WT=tree COMMIT={SHA} TESTS=481 passed")

    assert caught.value.field == "TESTS"


@pytest.mark.parametrize(
    "line",
    [
        f"EVENT=LAND_PENDING TASK=#7 SHA={SHA} TASK_TITLE=t NOTE=receipt_pending",
        f"Prose EVENT=CANDIDATE_WITHDRAWN TASK=#7 SHA={SHA}",
        "plain message with no protocol line",
        # Park and resume lines are checked by candidate_event, not this grammar.
        f"EVENT=CANDIDATE_RESUMED TASK=#7 SHA={SHA} TASK_TITLE=t extra words",
        # Retired review-only completion is not consumed.
        f"EVENT=REVIEW_COMPLETE TASK=#7 SHA={SHA} VERDICT=LAND LANDING=NOT_REQUIRED NOTE=",
        # Retired landing reports are not consumed.
        f"EVENT=LANDED TASK=#7 TASK_TITLE=Seat proofs SHA={SHA} RECEIPT=r NOTE=x",
        f"EVENT=ALREADY_LANDED TASK=#7 SHA={SHA} RECEIPT=r LANDED_BY_SESSION_ID=s NOTE=",
        # A retired proof result rides a resent CANDIDATE's NOTE instead.
        f"EVENT=PROOF_RESULT TASK=#7 SHA={SHA}ab RETEST=PASS NOTE=green",
    ],
)
def test_lines_without_a_consumed_event_are_unaffected(line: str) -> None:
    assert _parse(line) is None


def test_well_formed_proof_request_parses() -> None:
    fields = _parse(f"EVENT=PROOF_REQUEST TASK=#7 SHA={SHA} NOTE=prove it now")

    assert fields is not None
    assert fields["SHA"] == SHA
    assert fields["TASK"] == "#7"


@dataclass
class _Message:
    content: str
    to_session: str = "s-caller"


class _Sessions:
    def __init__(self, db: object) -> None:
        pass

    def get(self, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(project_id="p")


class _Tasks:
    def __init__(self, db: object) -> None:
        pass

    def resolve_task_reference(self, ref: str, project_id: str) -> str:
        return "task-id"

    def get_task(self, task_id: str) -> str:
        return task_id


@pytest.mark.parametrize("prefix", ["LANE= ", "LANE=2 "])
def test_candidate_lane_prefix_is_read_by_the_receiver(
    prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sender's grammar and the receiver's parser agree on an empty or numbered lane."""
    from gobby.workflows.engine import seat_messages

    monkeypatch.setattr(seat_messages, "SessionManager", _Sessions)
    monkeypatch.setattr(seat_messages, "LocalTaskManager", _Tasks)
    line = f"{prefix}EVENT=CANDIDATE TASK=#7 RUN=run WT=tree COMMIT={SHA} NOTE=ready"

    parsed = candidate_event(MagicMock(), cast(InterSessionMessage, _Message(line)))

    assert parsed is not None
    assert parsed.sha == SHA


@pytest.mark.parametrize("event", ["CANDIDATE_UPDATE", "CANDIDATE_VERDICT_EXTRA", "CANDIDATE_"])
def test_unknown_candidate_event_is_rejected_by_receiver(event: str) -> None:
    message = cast(InterSessionMessage, _Message(f"EVENT={event} TASK=#7 COMMIT={SHA}"))

    with pytest.raises(CandidateMessageError, match="expected EVENT=CANDIDATE TASK="):
        candidate_event(MagicMock(), message)


@pytest.mark.parametrize("event", ["CANDIDATE_WITHDRAWN", "CANDIDATE_RESUMED", "CANDIDATE_PARKED"])
def test_malformed_coordination_reports_event_grammar(event: str) -> None:
    message = cast(InterSessionMessage, _Message(f"EVENT={event} TASK=#7 SHA=abc1234"))

    with pytest.raises(CandidateMessageError, match=re.escape(_COORDINATION_GRAMMAR[event])):
        candidate_event(MagicMock(), message)
