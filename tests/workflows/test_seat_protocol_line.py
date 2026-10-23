"""Shared protocol-line grammar for the seat envelopes that transitions consume."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from gobby.storage.inter_session_messages import InterSessionMessage
from gobby.workflows.engine.seat_messages import CandidateMessageError, candidate_event

pytestmark = pytest.mark.unit

SHA = "0123456789abcdef0123456789abcdef01234567"
LANDED_CANONICAL = (
    "SHA BRANCH RECEIPT MODE LANDED_TIP ACTIVATION "
    "PROJECT_SYNC_REQUIRED PROVENANCE RETEST TASK_TITLE NOTE"
)


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
        (
            f"EVENT=LANDED TASK=#7 SHA={SHA} TASK_TITLE=Seat proofs BRANCH=b RECEIPT=r "
            "MODE=merge LANDED_TIP=t ACTIVATION=none PROJECT_SYNC_REQUIRED=false "
            "PROVENANCE=p RETEST=PASS NOTE=landed"
        ),
        f"EVENT=LANDED TASK=#7 SHA={SHA} RECEIPT=r TASK_TITLE=Seat BRANCH=b NOTE=x",
        f"EVENT=ALREADY_LANDED TASK=#7 SHA={SHA} TASK_TITLE=Seat RECEIPT=r LANDED_BY_SESSION_ID=s NOTE=",
        f"EVENT=REVIEW_COMPLETE TASK=#7 SHA={SHA} VERDICT=LAND LANDING=NOT_REQUIRED NOTE=",
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


def test_landed_retest_and_note_are_read() -> None:
    fields = _parse(f"EVENT=LANDED TASK=#7 SHA={SHA} RETEST=PASS NOTE=landed twice")

    assert fields is not None
    assert fields["RETEST"] == "PASS"
    assert fields["SHA"] == SHA
    assert fields["NOTE"] == "landed twice"


def test_candidate_run_with_whitespace_names_run() -> None:
    with pytest.raises(CandidateMessageError) as caught:
        _parse(f"EVENT=CANDIDATE TASK=#7 RUN=green pass WT=tree COMMIT={SHA}")

    assert caught.value.field == "RUN"
    assert caught.value.canonical == "TASK_TITLE RUN WT COMMIT NOTE"
    assert "TASK_TITLE RUN WT COMMIT NOTE" in str(caught.value)


def test_landed_with_title_before_sha_names_sha() -> None:
    with pytest.raises(CandidateMessageError) as caught:
        _parse(f"EVENT=LANDED TASK=#7 TASK_TITLE=Seat proofs SHA={SHA} RECEIPT=r NOTE=x")

    assert caught.value.field == "SHA"
    assert caught.value.canonical == LANDED_CANONICAL
    assert LANDED_CANONICAL in str(caught.value)


@pytest.mark.parametrize(
    ("line", "field"),
    [
        (f"EVENT=CANDIDATE TASK=#7 RUN=run WT=tree COMMIT={SHA[:10]}", "COMMIT"),
        (f"EVENT=CANDIDATE TASK=#7 RUN=run WT=tree COMMIT={SHA} RUN=other", "RUN"),
        (f"EVENT=CANDIDATE TASK=#7 WT=tree COMMIT={SHA}", "RUN"),
        (f"EVENT=CANDIDATE RUN=run WT=tree COMMIT={SHA}", "TASK"),
        (f"EVENT=CANDIDATE TASK=#7 RUN=run WT=tree COMMIT={SHA} TASK=#8", "TASK"),
        ("EVENT=LANDED TASK=#7 RECEIPT=r NOTE=x", "SHA"),
        (f"EVENT=LANDED TASK=#7 SHA={SHA} BOGUS=1 NOTE=x", "BOGUS"),
        (f"EVENT=CANDIDATE_VERDICT TASK=#7 SHA={SHA} TASK_TITLE=t VERDICT=BOUNCE", "VERDICT"),
        (f"EVENT=CANDIDATE_WITHDRAWN TASK=#7 SHA={SHA} BOGUS=1", "BOGUS"),
        (f"EVENT=CANDIDATE_VERDICT TASK=#7 SHA={SHA} VERDICT=BOUNCE SHA={SHA}", "SHA"),
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
        "EVENT=PROOF_REQUEST TASK=#7 NOTE=prove it now",
        f"Prose EVENT=CANDIDATE_WITHDRAWN TASK=#7 SHA={SHA}",
        "plain message with no protocol line",
        # Retired park/resume grammar is not validated at send time.
        f"EVENT=CANDIDATE_RESUMED TASK=#7 SHA={SHA} TASK_TITLE=t extra words",
    ],
)
def test_lines_without_a_consumed_event_are_unaffected(line: str) -> None:
    assert _parse(line) is None


TEMPLATE = Path(__file__).resolve().parents[2] / (
    "src/gobby/install/shared/skills/gobby/references/agents/procedures/code-reviewer.md"
)


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


def _landing_templates() -> dict[str, str]:
    text = TEMPLATE.read_text(encoding="utf-8")
    lines = re.findall(r"`(EVENT=(?:LAND_PENDING|ALREADY_LANDED|LANDED) [^`]*)`", text)
    return {line.split(" ", 1)[0].removeprefix("EVENT="): line for line in lines}


def _filled(line: str) -> str:
    """Give each empty template value a sample, keeping the template's field order."""
    tokens = []
    for token in line.split(" "):
        key, separator, value = token.partition("=")
        if not separator:
            tokens.append(token)
            continue
        if key == "TASK":
            value = "#7"
        elif key == "SHA":
            value = SHA
        else:
            value = value.split("|", 1)[0] or f"{key.lower()}-value"
        tokens.append(f"{key}={value}")
    return " ".join(tokens)


def test_landing_templates_cover_every_landing_event() -> None:
    assert set(_landing_templates()) == {"LAND_PENDING", "ALREADY_LANDED", "LANDED"}


@pytest.mark.parametrize("event", ["LAND_PENDING", "ALREADY_LANDED", "LANDED"])
def test_landing_templates_put_title_just_before_note(event: str) -> None:
    keys = [token.partition("=")[0] for token in _landing_templates()[event].split(" ")]

    assert keys[-2:] == ["TASK_TITLE", "NOTE"]


@pytest.mark.parametrize("event", ["ALREADY_LANDED", "LANDED"])
def test_landing_template_examples_read_every_field(
    event: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.workflows.engine import seat_messages

    filled = _filled(_landing_templates()[event])
    monkeypatch.setattr(seat_messages, "SessionManager", _Sessions)
    monkeypatch.setattr(seat_messages, "LocalTaskManager", _Tasks)

    message = cast(InterSessionMessage, _Message(filled))
    parsed = candidate_event(MagicMock(), message)

    assert parsed is not None
    assert {token.partition("=")[0] for token in filled.split(" ")} <= set(parsed.fields)
    assert parsed.sha == SHA


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
