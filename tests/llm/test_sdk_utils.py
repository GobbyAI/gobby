"""Tests for additionalContext paging: whole contributors ship in order, oversized ones page."""

from __future__ import annotations

import re

import pytest

from gobby.llm import sdk_utils
from gobby.llm.sdk_utils import (
    ADDITIONAL_CONTEXT_LIMIT,
    ContextDelivery,
    ContextPart,
    ContextPiece,
    deliver_context,
)

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


_ANY_PAGE_HEADER = re.compile(r"\[[^\]\n]* \(page \d+/\d+\)\]\n")
_LEADING_PAGE_NUMBER = re.compile(r"\A\[[^\]\n]* \(page (\d+)/\d+\)\]\n")
_BREADCRUMB = re.compile(r"(?:\n\n)?context continues on the next delivery: [^\n]*\Z")


def _reassemble_serialized(texts: list[str]) -> str:
    """Rebuild the original bytes from each delivery's serialized text, not its shipped tuples.

    Each delivery's text loses its breadcrumb and page headers and nothing else. Within a
    delivery the text already carries its blank lines. Between deliveries a whole part starts
    on a blank line, and a continuation page joins the page before it directly.
    """
    pieces: list[str] = []
    for text in texts:
        body = _BREADCRUMB.sub("", text)
        if not body:
            continue
        leading = _LEADING_PAGE_NUMBER.match(body)
        continues = leading is not None and int(leading.group(1)) > 1
        if pieces and not continues:
            pieces.append("\n\n")
        pieces.append(_ANY_PAGE_HEADER.sub("", body))
    return "".join(pieces)


def _deliver_all(parts: list[tuple[str, str]], limit: int) -> list[ContextDelivery]:
    deliveries: list[ContextDelivery] = []
    queue: list[ContextPart | ContextPiece] = list(parts)
    while queue:
        delivery = deliver_context(queue, limit=limit)
        assert delivery.shipped, "every delivery ships at least one part"
        deliveries.append(delivery)
        queue = list(delivery.remaining)
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
    assert [(piece.part, piece.body) for piece in delivery.remaining] == [
        ("rule:b", "b" * 60),
        ("rule:c", "c"),
    ]


def test_breadcrumb_names_are_bounded_and_every_queued_part_still_waits() -> None:
    parts = [(f"rule:item-{index:04d}", "x" * 100) for index in range(300)]

    delivery = deliver_context(parts, limit=ADDITIONAL_CONTEXT_LIMIT)

    assert len(delivery.text) <= ADDITIONAL_CONTEXT_LIMIT
    assert "(+" in delivery.text and "more)" in delivery.text
    assert len(delivery.shipped) + len(delivery.remaining) == 300
    assert (delivery.remaining[0].part, delivery.remaining[0].body) == parts[len(delivery.shipped)]


def test_oversized_part_pages_at_paragraph_boundaries_with_numbered_labels() -> None:
    paragraphs = [f"paragraph {index} " + "p" * 25 for index in range(6)]
    body = "\n\n".join(paragraphs)

    deliveries = _deliver_all([("rule:big", body)], limit=200)

    shipped = [entry for delivery in deliveries for entry in delivery.shipped]
    assert len(shipped) > 1
    assert [label for label, _ in shipped] == [
        f"rule:big (page {index}/{len(shipped)})" for index in range(1, len(shipped) + 1)
    ]
    for delivery in deliveries:
        assert len(delivery.text) <= 200
    assert _reassemble(shipped) == body
    assert _reassemble_serialized([delivery.text for delivery in deliveries]) == body


def test_three_times_the_cap_with_an_oversized_part_arrives_in_order_and_whole() -> None:
    limit = 1_000
    big = "\n\n".join(f"section {index} " + "s" * 260 for index in range(12))
    parts = [("rule:a", "a" * 400), ("rule:big", big), ("rule:c", "c" * 300)]

    deliveries = _deliver_all(parts, limit)

    shipped = [entry for delivery in deliveries for entry in delivery.shipped]
    expected = "a" * 400 + "\n\n" + big + "\n\n" + "c" * 300
    assert len(deliveries) >= 3
    for delivery in deliveries:
        assert len(delivery.text) <= limit
    assert _reassemble(shipped) == expected
    assert _reassemble_serialized([delivery.text for delivery in deliveries]) == expected
    whole_labels = [label for label, _ in shipped if "(page " not in label]
    assert whole_labels == ["rule:a", "rule:c"]


def test_whitespace_free_run_larger_than_the_limit_fails_loudly_and_is_never_cut() -> None:
    run = "x" * 1_250

    with pytest.raises(sdk_utils.UnpageableContextError, match="1250-character run"):
        deliver_context([("rule:run", run)], limit=300)


def test_a_limit_too_small_to_hold_a_page_and_its_breadcrumb_fails_loudly() -> None:
    with pytest.raises(ValueError, match="too small to page rule:run"):
        deliver_context([("rule:run", "x " * 100)], limit=100)


def test_the_derived_minimum_is_the_smallest_limit_that_holds_the_longest_label() -> None:
    # 375 = the longest label (200) plus a six-digit page header and one body character.
    assert sdk_utils.MAX_CONTEXT_LABEL_CHARS == 200
    assert sdk_utils.minimum_additional_context_limit() == 375


def test_the_longest_label_pages_within_the_derived_minimum() -> None:
    label = "rule:" + "x" * (sdk_utils.MAX_CONTEXT_LABEL_CHARS - len("rule:"))
    limit = sdk_utils.minimum_additional_context_limit()
    body = " ".join(["word"] * 100)

    deliveries = _deliver_all([(label, body)], limit)

    assert len(deliveries) > 1
    for delivery in deliveries:
        assert len(delivery.text) <= limit
    assert _reassemble_serialized([delivery.text for delivery in deliveries]) == body


@pytest.mark.parametrize(
    ("limit", "paragraphs", "paragraph_chars", "late_parts", "late_chars"),
    [(200, 4, 60, 4, 44), (300, 39, 21, 3, 16), (600, 51, 120, 4, 84)],
)
def test_a_queued_page_never_nests_its_header_when_later_parts_join_the_queue(
    limit: int, paragraphs: int, paragraph_chars: int, late_parts: int, late_chars: int
) -> None:
    """A carried page keeps one header even after later parts lengthen its breadcrumb."""
    body = "\n\n".join(f"p{index} " + "x" * paragraph_chars for index in range(paragraphs))
    queue: list[ContextPart | ContextPiece] = [("rule:big", body)]
    shipped: list[tuple[str, str]] = []
    texts: list[str] = []
    for turn in range(200):
        if turn < late_parts:
            queue.append((f"rule:late-{turn}", "l" * late_chars))
        if not queue:
            break
        delivery = deliver_context(queue, limit=limit)
        assert delivery.shipped, "every delivery ships at least one part"
        shipped.extend(delivery.shipped)
        texts.append(delivery.text)
        queue = list(delivery.remaining)
    else:
        pytest.fail("paging must terminate")

    for label, _ in shipped:
        assert label.count(" (page ") <= 1, label
    big = [entry for entry in shipped if entry[0].startswith("rule:big")]
    assert _reassemble(big) == body
    late = "".join("\n\n" + "l" * late_chars for _ in range(late_parts))
    assert _reassemble_serialized(texts) == body + late


@pytest.mark.parametrize(("first_limit", "later_limit"), [(1_000, 600), (600, 300), (2_000, 400)])
def test_a_queued_page_cut_again_after_the_limit_shrinks_keeps_one_header(
    first_limit: int, later_limit: int
) -> None:
    """A limit that shrinks between deliveries cuts the queued pages again from raw text."""
    body = "\n\n".join(f"p{index} " + "x" * 60 for index in range(40))
    first = deliver_context([("rule:big", body)], limit=first_limit)
    shipped = list(first.shipped)
    texts = [first.text]
    queue = first.remaining
    for _ in range(200):
        if not queue:
            break
        delivery = deliver_context(queue, limit=later_limit)
        assert delivery.shipped, "every delivery ships at least one part"
        assert len(delivery.text) <= later_limit
        shipped.extend(delivery.shipped)
        texts.append(delivery.text)
        queue = list(delivery.remaining)
    else:
        pytest.fail("paging must terminate")

    for label, _ in shipped:
        assert label.count(" (page ") <= 1, label
    assert _reassemble(shipped) == body
    assert _reassemble_serialized(texts) == body


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


_BIG_BODY = "\n\n".join(f"p{index} " + "x" * 60 for index in range(40))


def _rebuild_labeled_part(label: str, texts: list[str]) -> str:
    """Rebuild one labeled part from its serialized deliveries, matching its label literally.

    Page numbers across all deliveries must run 1, 2, 3 with no repeat or gap, so a header
    nested inside another page's body fails here even though its text matches the label.
    """
    header = re.compile(r"\[" + re.escape(label) + r" \(page (\d+)/\d+\)\]\n")
    bodies = [_BREADCRUMB.sub("", text) for text in texts]
    numbers = [int(number) for body in bodies for number in header.findall(body)]
    assert numbers == list(range(1, len(numbers) + 1)), numbers
    return "".join(header.sub("", body) for body in bodies)


def _drain(parts: list[tuple[str, str]], first_limit: int, later_limit: int) -> list[str]:
    """Deliver ``parts`` at ``first_limit``, then drain the queue at ``later_limit``."""
    first = deliver_context(parts, limit=first_limit)
    texts = [first.text]
    queue = first.remaining
    for _ in range(200):
        if not queue:
            return texts
        delivery = deliver_context(queue, limit=later_limit)
        assert delivery.shipped, "every delivery ships at least one part"
        assert len(delivery.text) <= later_limit
        texts.append(delivery.text)
        queue = list(delivery.remaining)
    pytest.fail("paging must terminate")


@pytest.mark.parametrize(("first_limit", "later_limit"), [(1_000, 600), (600, 300), (2_000, 400)])
def test_a_label_holding_a_closing_bracket_rebuilds_byte_for_byte_as_the_limit_shrinks(
    first_limit: int, later_limit: int
) -> None:
    """A ']' in a label must not stop its page header from being recognized on the next cut."""
    label = "rule:fixture]"
    texts = _drain([(label, _BIG_BODY)], first_limit, later_limit)
    assert len(texts) > 1
    assert _rebuild_labeled_part(label, texts) == _BIG_BODY


def test_a_raw_part_labeled_like_a_page_joins_as_a_whole_part() -> None:
    """Page state comes from queued page metadata, never from a label that reads like a page."""
    delivery = deliver_context([("rule:plain", "first"), ("rule:fixture (page 2/3)", "second")])
    assert delivery.text == "first\n\nsecond"
    assert delivery.remaining == []


@pytest.mark.parametrize(("first_limit", "later_limit"), [(1_000, 600), (600, 300)])
def test_an_oversized_raw_part_labeled_like_a_page_pages_under_its_own_label(
    first_limit: int, later_limit: int
) -> None:
    """A raw part whose label reads like a page is numbered from 1 under its own label."""
    label = "rule:fixture (page 2/3)"
    texts = _drain([(label, _BIG_BODY)], first_limit, later_limit)
    assert len(texts) > 1
    assert _rebuild_labeled_part(label, texts) == _BIG_BODY
