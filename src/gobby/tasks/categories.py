"""Task category constants shared outside storage."""

from __future__ import annotations

# Categories whose manifest entries may opt into deterministic TDD wrapping.
TDD_ELIGIBLE_CATEGORIES: frozenset[str] = frozenset({"code", "config"})

# Code implementation routing domains. These route software implementation
# leaves without overloading the task category field.
IMPLEMENTATION_DOMAINS: frozenset[str] = frozenset({"backend", "frontend", "fullstack"})
AGENT_BY_IMPLEMENTATION_DOMAIN: dict[str, str] = {
    "backend": "backend-developer",
    "frontend": "frontend-developer",
    "fullstack": "fullstack-developer",
}

# Categories that expansion may emit as executable leaves. These all start at
# development in build stage manifests.
DEVELOPMENT_FORWARD_LEAF_CATEGORIES: frozenset[str] = frozenset(
    {"code", "config", "docs", "refactor", "test"}
)

# Categories the build lifecycle can automate as direct leaf targets. Expansion
# output is intentionally narrower; use DEVELOPMENT_FORWARD_LEAF_CATEGORIES.
AUTOMATED_LEAF_CATEGORIES: frozenset[str] = frozenset(
    DEVELOPMENT_FORWARD_LEAF_CATEGORIES | {"planning", "research"}
)

__all__ = [
    "AGENT_BY_IMPLEMENTATION_DOMAIN",
    "AUTOMATED_LEAF_CATEGORIES",
    "DEVELOPMENT_FORWARD_LEAF_CATEGORIES",
    "IMPLEMENTATION_DOMAINS",
    "TDD_ELIGIBLE_CATEGORIES",
]
