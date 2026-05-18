from __future__ import annotations


def parse_tags(value: str | None, *, empty_as_none: bool = False) -> list[str] | None:
    if value is None or value == "":
        return None

    tags = [tag.strip() for tag in value.split(",") if tag.strip()]
    if empty_as_none and not tags:
        return None
    return tags


def format_tags(tags: list[str]) -> str:
    return f" [{', '.join(tags)}]" if tags else ""


def truncate(value: str, limit: int) -> str:
    return f"{value[:limit]}{'...' if len(value) > limit else ''}"
