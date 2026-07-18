"""Small SQL string helpers for dynamically sized bound-parameter lists."""

from __future__ import annotations


def sql_placeholders(count: int, separator: str = ",") -> str:
    """Return a placeholder list for bound SQL parameters."""

    if count < 1:
        raise ValueError("count must be greater than or equal to 1")
    return separator.join("%s" for _ in range(count))


def render_internal_sql(template: str, /, **fragments: str) -> str:
    """Render allowlisted SQL structure while external values stay bound parameters."""
    forbidden = ("\x00", ";", "--", "/*", "*/")
    if any(token in fragment for fragment in fragments.values() for token in forbidden):
        raise ValueError("trusted SQL fragment contains a statement delimiter or comment")
    return template.format_map(fragments)


__all__ = ["sql_placeholders"]
