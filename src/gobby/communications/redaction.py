"""Redacted, length-bounded message text for phone-readable alerts."""

from __future__ import annotations

from gobby.utils.terminal_output import redact_terminal_output

TRUNCATION_MARKER = "\n…(truncated)"


def redact_and_bound(text: str, max_chars: int) -> str:
    """Scrub secrets from ``text``, then cut it to at most ``max_chars`` characters.

    Redaction runs before truncation so a cut can never leave a secret fragment
    too short for the redaction patterns to recognize.
    """
    if max_chars <= len(TRUNCATION_MARKER):
        raise ValueError(f"max_chars must exceed {len(TRUNCATION_MARKER)}")
    redacted = redact_terminal_output(text)
    if len(redacted) <= max_chars:
        return redacted
    return redacted[: max_chars - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


MAX_LOG_ATTACHMENT_BYTES = 64 * 1024


def redact_for_attachment(text: str) -> str | None:
    """Scrub secrets from ``text`` for a document attachment.

    Returns ``None`` when the redacted UTF-8 content exceeds
    ``MAX_LOG_ATTACHMENT_BYTES``; the caller omits the attachment rather than
    sending a cut log.
    """
    redacted = redact_terminal_output(text)
    if len(redacted.encode("utf-8")) > MAX_LOG_ATTACHMENT_BYTES:
        return None
    return redacted
