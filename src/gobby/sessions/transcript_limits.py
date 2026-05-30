"""Shared transcript serving limits.

Pinned in one place so the route, reader, and MCP tools clamp identically. The
rendered/legacy caps bound how many groups/rows a single request can render;
``NATIVE_JSON_MAX_BYTES`` bounds the only non-windowable path.
"""

from __future__ import annotations

#: Max rendered-group ``limit`` a single request may ask for (clamped, not 422).
RENDERED_LIMIT_MAX = 200
#: Max legacy flat-row ``limit`` a single request may ask for (clamped, not 422).
LEGACY_LIMIT_MAX = 500
#: Native-JSON transcripts larger than this are refused (HTTP 413, download instead).
NATIVE_JSON_MAX_BYTES = 32 * 1024 * 1024

__all__ = [
    "LEGACY_LIMIT_MAX",
    "NATIVE_JSON_MAX_BYTES",
    "RENDERED_LIMIT_MAX",
]
