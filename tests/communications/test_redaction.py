"""Tests for redacted, length-bounded alert text."""

from __future__ import annotations

import pytest

from gobby.communications.redaction import (
    MAX_LOG_ATTACHMENT_BYTES,
    TRUNCATION_MARKER,
    redact_and_bound,
    redact_for_attachment,
)

pytestmark = pytest.mark.unit

SECRET = "abcdefghijklmnopqrstuvwxyz0123"


def test_short_text_is_redacted_without_truncation() -> None:
    text = f"errors.log: api_key={SECRET}"

    assert redact_and_bound(text, 200) == "errors.log: api_key=<redacted>"


@pytest.mark.parametrize("chars_before_cut", [4, 8, 11, 20])
def test_secret_straddling_the_cut_is_never_partially_sent(chars_before_cut: int) -> None:
    """Truncating first would leave a fragment too short for the patterns to catch."""
    max_chars = 120
    cut = max_chars - len(TRUNCATION_MARKER)
    prefix = "x" * (cut - len(" token=") - chars_before_cut)
    text = f"{prefix} token={SECRET} trailing log context " + "y" * 200

    result = redact_and_bound(text, max_chars)

    assert len(result) == max_chars
    assert SECRET[:4] not in result
    assert "token=<" in result


@pytest.mark.parametrize("chars_before_cut", [2, 6, 12])
def test_quoted_short_secret_straddling_the_cut_is_never_partially_sent(
    chars_before_cut: int,
) -> None:
    max_chars = 80
    cut = max_chars - len(TRUNCATION_MARKER)
    prefix = "x" * (cut - len(' body={"password":') - chars_before_cut)
    text = f'{prefix} body={{"password":"hunter2 secret"}} ' + "y" * 200

    result = redact_and_bound(text, max_chars)

    assert len(result) == max_chars
    assert "hunt" not in result
    assert '"password":<' in result


@pytest.mark.parametrize("chars_before_cut", [2, 6, 12])
def test_escaped_quote_secret_straddling_the_cut_is_never_partially_sent(
    chars_before_cut: int,
) -> None:
    max_chars = 80
    cut = max_chars - len(TRUNCATION_MARKER)
    prefix = "x" * (cut - len(' body={"password":') - chars_before_cut)
    text = f'{prefix} body={{"password":"ab\\"secretTAIL"}} ' + "y" * 200

    result = redact_and_bound(text, max_chars)

    assert len(result) == max_chars
    assert "secretTAIL" not in result
    assert "TAIL" not in result
    assert '"password":<' in result


@pytest.mark.parametrize("max_chars", [64, 500, 4096])
def test_result_never_exceeds_the_bound(max_chars: int) -> None:
    text = "\n".join(f"2026-09-28 01:{i % 60:02d}:00 - ERROR - line {i}" for i in range(2000))

    result = redact_and_bound(text, max_chars)

    assert len(result) == max_chars
    assert result.endswith(TRUNCATION_MARKER)


def test_bound_must_leave_room_for_content() -> None:
    with pytest.raises(ValueError, match="max_chars must exceed"):
        redact_and_bound("anything", len(TRUNCATION_MARKER))


def test_attachment_content_is_redacted_without_truncation() -> None:
    text = f"line one\napi_key={SECRET}\n" + "y" * 5000

    assert redact_for_attachment(text) == "line one\napi_key=<redacted>\n" + "y" * 5000


def test_attachment_at_cap_passes_and_one_byte_over_is_omitted() -> None:
    assert redact_for_attachment("x" * MAX_LOG_ATTACHMENT_BYTES) == "x" * MAX_LOG_ATTACHMENT_BYTES
    assert redact_for_attachment("x" * (MAX_LOG_ATTACHMENT_BYTES + 1)) is None


def test_attachment_cap_counts_utf8_bytes() -> None:
    # 3 bytes per character: under the cap in characters, over it in bytes.
    text = "✓" * (MAX_LOG_ATTACHMENT_BYTES // 3 + 1)

    assert len(text) < MAX_LOG_ATTACHMENT_BYTES
    assert redact_for_attachment(text) is None
