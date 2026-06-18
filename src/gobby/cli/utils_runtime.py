"""Runtime accessors for the CLI utils compatibility facade."""

from __future__ import annotations

from typing import Any


def facade() -> Any:
    """Return the live ``gobby.cli.utils`` module.

    The facade is intentionally dynamic because the CLI tests and some callers
    patch names on ``gobby.cli.utils`` directly. Implementation modules call
    back through this accessor when they need one of those patchable names.
    """
    from gobby.cli import utils

    return utils
