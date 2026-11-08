"""Tests for additionalContext paging: whole contributors ship in order, oversized ones page."""

from __future__ import annotations

import re

import pytest

from gobby.llm import sdk_utils
from gobby.llm.sdk_utils import ADDITIONAL_CONTEXT_LIMIT, Carry, ContextDelivery, deliver_context

pytestmark = pytest.mark.unit

_PAGE_HEADER = re.compile(r"^\[[^\]\n]* \(page \d+/\d+\)\]\n")
_PAGE_NUMBER = re.compile(r" \(page (\d+)/\d+\)$")


def _is_continuation(label: str) -> bool:
    match = _PAGE_NUMBER.search(label)
    return match is not None and int(match.group(1)) > 1


def _reassemble(shipped: list[tuple[str, str]]) -> str:
    """Rebuild the original bytes: pages join directly, whole contributors join on a blank line."""
    text = ""
    for index, (label, body) in enumerate(shipped):
        if index and not _is_continuation(label):
            text += "\n\n"
        text += _PAGE_HEADER.sub("", body, count=1)
    return text


def _deliver_all(parts: list[tuple[str, str]], limit: int) -> list[ContextDelivery]:
    deliveries: list[ContextDelivery] = []
    queue = list(parts)
    while queue:
        delivery = deliver_context(queue, limit=limit)
        assert delivery.shipped, "every delivery ships at least one part"
        deliveries.append(delivery)
        queue = delivery.remaining
        assert len(deliveries) < 200, "paging must terminate"
    return deliveries


def test_under_limit_ships_every_non_empty_part_in_order() -> None:
    delivery = deliver_context(
        [("claimed_tasks", "claimed #1"), ("rule:empty", ""), ("skill:brevity", "be brief")],
        limit=100,
    )

    assert delivery.text == "claimed #1\n\nbe brief"
    assert delivery.remaining == []


def test_first_part_that_does_not_fit_waits_and_nothing_behind_it_jumps_ahead() -> None:
    delivery = deliver_context(
        [("rule:a", "a" * 40), ("rule:b", "b" * 60), ("rule:c", "c")],
        limit=100,
    )

    assert delivery.text == "a" * 40 + "\n\ncontext continues on the next delivery: rule:b, rule:c"
    assert delivery.remaining == [("rule:b", "b" * 60), ("rule:c", "c")]


def test_a_delivery_that_cannot_queue_its_waiting_parts_says_they_were_not_carried() -> None:
    delivery = deliver_context(
        [("rule:a", "a" * 40), ("rule:b", "b" * 60)],
        limit=100,
        carry=Carry.UNQUEUED,
    )

    assert delivery.text == "a" * 40 + "\n\ncontext not carried, no session queue: rule:b"
    assert delivery.remaining == [("rule:b", "b" * 60)]


def test_breadcrumb_names_are_bounded_and_every_queued_part_still_waits() -> None:
    parts = [(f"rule:item-{index:04d}", "x" * 100) for index in range(300)]

    delivery = deliver_context(parts, limit=ADDITIONAL_CONTEXT_LIMIT)

    assert len(delivery.text) <= ADDITIONAL_CONTEXT_LIMIT
    assert "(+" in delivery.text and "more)" in delivery.text
    assert len(delivery.shipped) + len(delivery.remaining) == 300
    assert delivery.remaining[0] == parts[len(delivery.shipped)]


def test_oversized_part_pages_at_paragraph_boundaries_with_numbered_labels() -> None:
    paragraphs = [f"paragraph {index} " + "p" * 25 for index in range(6)]
    body = "\n\n".join(paragraphs)

    deliveries = _deliver_all([("rule:big", body)], limit=120)

    shipped = [entry for delivery in deliveries for entry in delivery.shipped]
    assert len(shipped) > 1
    assert [label for label, _ in shipped] == [
        f"rule:big (page {index}/{len(shipped)})" for index in range(1, len(shipped) + 1)
    ]
    for delivery in deliveries:
        assert len(delivery.text) <= 120
    assert _reassemble(shipped) == body


def test_three_times_the_cap_with_an_oversized_part_arrives_in_order_and_whole() -> None:
    limit = 1_000
    big = "\n\n".join(f"section {index} " + "s" * 260 for index in range(12))
    parts = [("rule:a", "a" * 400), ("rule:big", big), ("rule:c", "c" * 300)]

    deliveries = _deliver_all(parts, limit)

    shipped = [entry for delivery in deliveries for entry in delivery.shipped]
    assert len(deliveries) >= 3
    for delivery in deliveries:
        assert len(delivery.text) <= limit
    assert _reassemble(shipped) == "a" * 400 + "\n\n" + big + "\n\n" + "c" * 300
    whole_labels = [label for label, _ in shipped if "(page " not in label]
    assert whole_labels == ["rule:a", "rule:c"]


def test_whitespace_free_run_larger_than_the_limit_still_pages_losslessly() -> None:
    run = "x" * 1_250

    deliveries = _deliver_all([("rule:run", run)], limit=300)

    shipped = [entry for delivery in deliveries for entry in delivery.shipped]
    assert all(len(delivery.text) <= 300 for delivery in deliveries)
    assert _reassemble(shipped) == run


def test_a_limit_too_small_to_hold_a_page_and_its_breadcrumb_fails_loudly() -> None:
    with pytest.raises(ValueError, match="too small to page rule:run"):
        deliver_context([("rule:run", "x" * 250)], limit=100)


@pytest.mark.parametrize(
    ("limit", "paragraphs", "paragraph_chars", "late_parts", "late_chars"),
    [(200, 4, 84, 4, 44), (300, 39, 21, 3, 16), (600, 51, 120, 4, 84)],
)
def test_a_queued_page_never_nests_its_header_when_later_parts_join_the_queue(
    limit: int, paragraphs: int, paragraph_chars: int, late_parts: int, late_chars: int
) -> None:
    """A carried page keeps one header even after later parts lengthen its breadcrumb."""
    body = "\n\n".join(f"p{index} " + "x" * paragraph_chars for index in range(paragraphs))
    queue: list[tuple[str, str]] = [("rule:big", body)]
    shipped: list[tuple[str, str]] = []
    for turn in range(200):
        if turn < late_parts:
            queue.append((f"rule:late-{turn}", "l" * late_chars))
        if not queue:
            break
        delivery = deliver_context(queue, limit=limit)
        assert delivery.shipped, "every delivery ships at least one part"
        shipped.extend(delivery.shipped)
        queue = delivery.remaining
    else:
        pytest.fail("paging must terminate")

    for label, _ in shipped:
        assert label.count(" (page ") <= 1, label
    big = [entry for entry in shipped if entry[0].startswith("rule:big")]
    assert _reassemble(big) == body


@pytest.mark.parametrize(("first_limit", "later_limit"), [(1_000, 600), (600, 300), (2_000, 400)])
def test_a_queued_page_cut_again_after_the_limit_shrinks_keeps_one_header(
    first_limit: int, later_limit: int
) -> None:
    """A limit that shrinks between deliveries cuts the queued pages again from raw text."""
    body = "\n\n".join(f"p{index} " + "x" * 60 for index in range(40))
    first = deliver_context([("rule:big", body)], limit=first_limit)
    shipped = list(first.shipped)
    queue = first.remaining
    for _ in range(200):
        if not queue:
            break
        delivery = deliver_context(queue, limit=later_limit)
        assert delivery.shipped, "every delivery ships at least one part"
        assert len(delivery.text) <= later_limit
        shipped.extend(delivery.shipped)
        queue = delivery.remaining
    else:
        pytest.fail("paging must terminate")

    for label, _ in shipped:
        assert label.count(" (page ") <= 1, label
    assert _reassemble(shipped) == body


def test_removed_length_cutting_helpers_stay_gone() -> None:
    for name in (
        "allocate_section_budget",
        "head_with_breadcrumb",
        "_clean_cut",
        "_trim_section",
        "split_markdown_sections",
        "fit_additional_context",
        "truncate_additional_context",
        "_truncate_contributors",
        "_persist_additional_context",
        "ContextFit",
    ):
        assert not hasattr(sdk_utils, name), name
