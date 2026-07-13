"""Shared secret-name normalization and reference grammar."""

from __future__ import annotations

import re

SECRET_NAME_GRAMMAR = r"[A-Za-z_][A-Za-z0-9_]*"
SECRET_NAME_PATTERN = re.compile(SECRET_NAME_GRAMMAR)
SECRET_REF_PATTERN = re.compile(rf"\$secret:({SECRET_NAME_GRAMMAR})")


def normalize_secret_name(name: str) -> str:
    """Return the canonical case-insensitive representation of a secret name."""
    return name.strip().lower()


def normalize_and_validate_secret_name(name: str) -> str:
    """Return a canonical name that can be represented by a secret reference."""
    normalized_name = normalize_secret_name(name)
    if SECRET_NAME_PATTERN.fullmatch(normalized_name) is None:
        raise ValueError(
            "Invalid secret name: must start with a letter or underscore and contain only "
            "letters, digits, and underscores"
        )
    return normalized_name
