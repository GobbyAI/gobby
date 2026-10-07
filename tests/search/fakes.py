"""Shared fakes for embeddings API tests."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from typing import Any


class RawEmbeddingsResponse:
    """Stand-in for ``with_raw_response.create()``: only the raw body is readable.

    ``parse()`` raises so tests fail if the SDK's per-float model construction runs.
    ``read_threads`` records the thread that read the body.
    """

    def __init__(self, items: Iterable[Any]) -> None:
        self._body = json.dumps(
            {"data": [{"index": item.index, "embedding": item.embedding} for item in items]}
        ).encode()
        self.read_threads: list[int] = []

    @property
    def content(self) -> bytes:
        self.read_threads.append(threading.get_ident())
        return self._body

    def parse(self) -> object:
        raise AssertionError("SDK response model construction used")
