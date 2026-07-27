from __future__ import annotations

import pytest

from gobby.communications.adapters.telegram_formatting import (
    markdown_to_telegram_html_chunks,
)

pytestmark = pytest.mark.unit


def test_deeply_nested_links_fall_back_without_recursion_error() -> None:
    content = "deep"
    for _ in range(32):
        content = f"[{content}](https://example.com)"

    chunks = markdown_to_telegram_html_chunks(content, 4096)

    assert chunks
    assert "deep" in "".join(chunks)


def test_trailing_formatting_tags_do_not_create_standalone_chunk() -> None:
    chunks = markdown_to_telegram_html_chunks("[abc ](https://example.com)", 3)

    assert chunks == ['<a href="https://example.com">abc</a>']
