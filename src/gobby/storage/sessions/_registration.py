"""Session registration helpers."""

from __future__ import annotations

from ._title_defaults import MANUAL_TITLE_SOURCE
from ._update_sentinel import UNSET, UnsetType, is_set


def manual_registration_title(
    title: str | None | UnsetType,
    title_source: str | None | UnsetType,
) -> tuple[str | UnsetType, str | UnsetType]:
    """Classify a non-empty registration title as user-owned."""
    if not is_set(title) or not isinstance(title, str) or not title.strip():
        return UNSET, UNSET
    if is_set(title_source) and title_source not in {None, MANUAL_TITLE_SOURCE}:
        return UNSET, UNSET
    return title.strip(), MANUAL_TITLE_SOURCE
