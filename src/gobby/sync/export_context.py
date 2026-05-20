"""JSONL export context guards."""

from __future__ import annotations

import os

JSONL_EXPORT_CONTEXT_ENV = "GOBBY_JSONL_EXPORT_CONTEXT"
_ALLOWED_JSONL_EXPORT_CONTEXTS = frozenset({"pre-push", "remote-push"})


def in_jsonl_export_context() -> bool:
    """Return True when tracked JSONL projections may be written."""
    return os.environ.get(JSONL_EXPORT_CONTEXT_ENV, "").strip() in _ALLOWED_JSONL_EXPORT_CONTEXTS
