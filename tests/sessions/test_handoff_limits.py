"""Authored handoffs have a wire-size bound before any persistence."""

import json

import pytest

from gobby.sessions.handoff_records import MAX_HANDOFF_CONTENT_CHARS, build_handoff_payload


@pytest.mark.parametrize("text", ['quoted "state"', "日本語 😀", "line\n" * 12])
def test_handoff_size_counts_escaping_and_rendered_headings(text: str) -> None:
    text = text.strip()
    payload = build_handoff_payload(current_state=text, next_steps=["Continue"])
    remaining = MAX_HANDOFF_CONTENT_CHARS - len(json.dumps(payload.rendered_markdown))
    accepted = build_handoff_payload(current_state=text + "x" * remaining, next_steps=["Continue"])
    assert len(json.dumps(accepted.rendered_markdown)) == MAX_HANDOFF_CONTENT_CHARS
    assert accepted.current_state == text + "x" * remaining
    with pytest.raises(ValueError, match="10001 JSON-escaped characters; limit is 10000"):
        build_handoff_payload(current_state=text + "x" * (remaining + 1), next_steps=["Continue"])


def test_optional_sections_share_the_same_total_budget() -> None:
    with pytest.raises(ValueError, match="Shorten the inline handoff and retry") as error:
        build_handoff_payload(
            current_state="Ready", next_steps=["Continue"], notes=["x" * 5_000] * 2
        )
    for instruction in (
        "including rendered formatting",
        "Remove cumulative history, copied reflections, and superseded detail",
        "relevant validation, and brief friction observations from this epoch",
        "create or update a Markdown session-notes file",
        "project-relative path to references",
        "Do not move cumulative history into that file",
    ):
        assert instruction in str(error.value)
