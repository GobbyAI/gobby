"""Shared transcript serving limits.

Pinned in one place so the route, reader, and MCP tools clamp identically. The
rendered and flat-row caps bound how many groups/rows a single request can render.
"""

from __future__ import annotations

#: Max rendered-group ``limit`` a single request may ask for (clamped, not 422).
RENDERED_LIMIT_MAX = 200
#: Max flat-row ``limit`` a single request may ask for (clamped, not 422).
FLAT_ROW_LIMIT_MAX = 500
__all__ = [
    "FLAT_ROW_LIMIT_MAX",
    "RENDERED_LIMIT_MAX",
]
