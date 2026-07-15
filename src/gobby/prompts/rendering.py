"""Shared safety helpers for rendering LLM prompts."""

from __future__ import annotations

from html import escape
from typing import Any


def delimit_untrusted_content(value: Any) -> str:
    """Wrap external prompt data in delimiters that the payload cannot close."""
    escaped_value = escape("" if value is None else str(value), quote=False)
    return f"<untrusted_content>\n{escaped_value}\n</untrusted_content>"
