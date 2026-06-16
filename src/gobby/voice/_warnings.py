"""Warning filters for noisy optional voice dependencies."""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def suppress_perth_pkg_resources_warning() -> Iterator[None]:
    """Suppress resemble-perth's import-time pkg_resources deprecation warning."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"pkg_resources is deprecated as an API\.",
            category=UserWarning,
        )
        yield
