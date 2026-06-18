"""Runtime access to the public agents facade."""

from __future__ import annotations

from importlib import import_module
from typing import Any


def facade() -> Any:
    """Return the public agents module so legacy patch points stay live."""
    return import_module("gobby.mcp_proxy.tools.agents")
