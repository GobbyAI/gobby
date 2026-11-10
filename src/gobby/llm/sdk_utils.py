"""Shared utilities for Claude Agent SDK integration.

Functions extracted from claude_streaming.py, chat_session.py, and
chat_session_helpers.py to eliminate duplication across SDK consumers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from typing import NamedTuple


def sanitize_error(e: Exception) -> str:
    """Return a user-facing error message, hiding internal library details."""
    msg = str(e)
    if "model isn't mapped" in msg or "custom_llm_provider" in msg:
        return "An internal error occurred. Please try again."
    return msg


def parse_server_name(full_tool_name: str) -> str:
    """Extract server name from mcp__{server}__{tool} format."""
    if full_tool_name.startswith("mcp__"):
        parts = full_tool_name.split("__")
        if len(parts) >= 2:
            return parts[1]
    return "builtin"


def format_exception_group(eg: ExceptionGroup) -> str:
    """Format an ExceptionGroup into a semicolon-separated error string."""
    errors = [sanitize_error(exc) for exc in eg.exceptions]
    return "; ".join(errors)


# Claude Code / Agent SDK hard-truncates additionalContext at 10K chars.
# We cap slightly below to avoid the ugly "... [output truncated]" suffix.
# Live overrides live in hooks.additional_context_limit / additional_context_limits.
ADDITIONAL_CONTEXT_LIMIT = 9_950

# Reserved under the ship limit for overflow instruction + adapter metadata.
INLINE_CONTEXT_HEADROOM = 450

# Most label characters the continuation breadcrumb names before it counts the rest.
CONTINUATION_LABEL_BUDGET = 400


_CONTINUATION_PREFIX = "context continues on the next delivery: "
# Finest boundary first: a page cuts at a paragraph, else a line, else a word.
_PAGE_SEPARATORS = ("\n\n", "\n", " ")

ContextPart = tuple[str, str]


class ContextPiece(NamedTuple):
    """One queued piece: a whole part (``number`` 0) or one numbered page of a part.

    ``part`` is the part's label and ``body`` its text, or a page's raw text without its header.
    Page state lives in ``number`` and ``total``; no label or text is parsed to recover it.
    """

    part: str
    body: str
    number: int = 0
    total: int = 0

    @property
    def label(self) -> str:
        """The label a delivery names this piece by."""
        return f"{self.part} (page {self.number}/{self.total})" if self.number else self.part

    @property
    def text(self) -> str:
        """The text a delivery ships for this piece, with the header a page carries."""
        return f"[{self.label}]\n{self.body}" if self.number else self.body


@dataclass(frozen=True)
class ContextDelivery:
    """One delivery: the rendered text, the parts it ships, and the ordered pieces that wait."""

    text: str
    shipped: list[ContextPart]
    remaining: list[ContextPiece]


class UnpageableContextError(ValueError):
    """A part holds a run no page can hold whole and no boundary can split. Nothing is cut."""


# Longest context label a page header carries. Rule names are bounded to it at load.
MAX_CONTEXT_LABEL_CHARS = 200
# Page numbers in a header count up to six digits, as the breadcrumb's queue count does.
_PAGE_NUMBER_DIGITS = 6


def _page_room(limit: int) -> int:
    """Characters a page body may use beside its header at ``limit``."""
    return limit - _breadcrumb_reserve(limit) - len("\n\n")


@cache
def minimum_additional_context_limit() -> int:
    """Smallest ship limit whose page holds the longest label, its header and one character.

    Derived, not fixed: the breadcrumb reserve grows with the limit, so the search runs over the
    limit itself. Config rejects a limit below this value; nothing is clamped to it.
    """
    digits = "9" * _PAGE_NUMBER_DIGITS
    header = len(f"[{'x' * MAX_CONTEXT_LABEL_CHARS} (page {digits}/{digits})]\n")
    limit = 1
    while _page_room(limit) - header < 1:
        limit += 1
    return limit


def deliver_context(
    parts: Sequence[ContextPart | ContextPiece],
    *,
    limit: int | None = None,
) -> ContextDelivery:
    """Ship the longest in-order run of whole parts that fits ``limit``; queue the rest.

    ``parts`` is an ordered sequence of ``(label, text)`` pairs, or queued pieces from an
    earlier delivery; empty texts are skipped. Nothing is dropped or reordered: the first part
    that does not fit ends the delivery, and it and everything behind it wait for the next one.
    A part too large to ship alone pages at paragraph boundaries into ``<label> (page i/n)``
    pieces, each with its own header, and those pages ship consecutively. The text ends with a
    breadcrumb naming the labels that continue on the next delivery.
    """
    ship_limit = ADDITIONAL_CONTEXT_LIMIT if limit is None else limit
    queue = [piece for piece in map(_piece, parts) if piece.body]
    shipped: list[ContextPiece] = []
    while queue:
        head = queue[0]
        rest = queue[1:]
        candidate = _render([*shipped, head], [piece.label for piece in rest], ship_limit)
        if len(candidate) <= ship_limit:
            shipped.append(queue.pop(0))
            continue
        if shipped:
            break
        count, part, first = _remainder(queue)
        queue[:count] = _page(part, ship_limit, first)
    waiting = [piece.label for piece in queue]
    rendered = [(piece.label, piece.text) for piece in shipped]
    return ContextDelivery(_render(shipped, waiting, ship_limit), rendered, queue)


def _piece(part: ContextPart | ContextPiece) -> ContextPiece:
    """A caller's ``(label, text)`` part, or a piece queued by an earlier delivery."""
    if isinstance(part, ContextPiece):
        return part
    return ContextPiece(part[0], part[1])


def _render(parts: Sequence[ContextPiece], waiting: Sequence[str], limit: int) -> str:
    body = _join(parts)
    breadcrumb = _continuation(waiting, limit)
    if not breadcrumb:
        return body
    return f"{body}\n\n{breadcrumb}" if body else breadcrumb


def _join(parts: Sequence[ContextPiece]) -> str:
    """Whole parts join on a blank line; a page after the first joins its predecessor directly."""
    text = ""
    for index, piece in enumerate(parts):
        continues_page = piece.number > 1
        if index and not continues_page:
            text += "\n\n"
        text += piece.text
    return text


def _label_budget(limit: int) -> int:
    """Characters the breadcrumb may spend naming waiting labels."""
    return min(CONTINUATION_LABEL_BUDGET, max(1, limit // 4))


def _breadcrumb_reserve(limit: int) -> int:
    """The longest breadcrumb any queue can produce at ``limit``.

    Named labels stay within the label budget, and the count suffix has room for a queue
    of up to six digits. A page leaves this room, so it still fits beside its breadcrumb
    after later parts join the queue and lengthen it.
    """
    longest_prefix = len(_CONTINUATION_PREFIX)
    return longest_prefix + _label_budget(limit) + len(" (+999999 more)")


def _continuation(waiting: Sequence[str], limit: int) -> str:
    """Name what continues on the next delivery."""
    if not waiting:
        return ""
    budget = _label_budget(limit)
    named: list[str] = []
    used = 0
    for label in waiting:
        cost = len(label) + (2 if named else 0)
        if used + cost > budget:
            break
        named.append(label)
        used += cost
    more = len(waiting) - len(named)
    names = ", ".join(named)
    if more:
        suffix = f"(+{more} more)"
        names = f"{names} {suffix}" if named else suffix
    return _CONTINUATION_PREFIX + names


def _remainder(queue: Sequence[ContextPiece]) -> tuple[int, ContextPiece, int]:
    """The head piece's unshipped rest as ``(queue entries, raw part, first page number)``.

    A page run is the consecutive queue entries that carry the head part's remaining pages,
    matched by their metadata. Those pages may have been cut at a larger limit than the
    current one, so the run is joined back to its raw text and cut again, numbering on from
    the head page.
    """
    head = queue[0]
    if not head.number:
        return 1, head, 1
    raw: list[str] = []
    for number in range(head.number, head.total + 1):
        index = number - head.number
        if index >= len(queue):
            break
        entry = queue[index]
        if (entry.part, entry.number, entry.total) != (head.part, number, head.total):
            break
        raw.append(entry.body)
    return len(raw), ContextPiece(head.part, "".join(raw)), head.number


def _page(part: ContextPiece, limit: int, first: int = 1) -> list[ContextPiece]:
    """Split one whole part into numbered pages that each fit ``limit`` beside any breadcrumb.

    Pages number from ``first``. Each page leaves room for the longest breadcrumb a queue can
    produce, so a queued page never outgrows its delivery when later parts join the queue.
    Pages are cut from raw text and carry their numbers as metadata, so no page is ever cut
    from another page's header.
    """
    room = _page_room(limit)
    budget = room
    while budget > 0:
        bodies = _split_to_budget(part.body, budget)
        last = first + len(bodies) - 1
        pages = [
            ContextPiece(part.part, body, number, last)
            for number, body in enumerate(bodies, start=first)
        ]
        excess = max(len(page.text) - room for page in pages)
        if excess <= 0:
            return pages
        budget -= excess
    raise ValueError(f"additionalContext limit {limit} is too small to page {part.part}")


def _split_to_budget(
    text: str, budget: int, separators: tuple[str, ...] = _PAGE_SEPARATORS
) -> list[str]:
    """Split ``text`` into consecutive pieces of at most ``budget`` characters.

    Each separator stays attached to the unit before it, so the pieces concatenate back
    to ``text`` exactly. A unit too large for ``budget`` splits again at the next finer
    separator. A run with no separator left that still exceeds ``budget`` raises
    UnpageableContextError: no character is ever cut by length.
    """
    if len(text) <= budget:
        return [text]
    if not separators:
        raise UnpageableContextError(
            f"{len(text)}-character run has no paragraph, line or word boundary "
            f"to page within a {budget}-character page budget"
        )
    separator, finer = separators[0], separators[1:]
    segments = text.split(separator)
    units: list[str] = []
    for index, segment in enumerate(segments):
        unit = segment + separator if index < len(segments) - 1 else segment
        if len(unit) > budget:
            units.extend(_split_to_budget(unit, budget, finer))
        else:
            units.append(unit)
    pieces: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + len(unit) > budget:
            pieces.append(current)
            current = ""
        current += unit
    if current:
        pieces.append(current)
    return pieces
