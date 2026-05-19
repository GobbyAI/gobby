"""Helpers for locating bundled postgres-pgsearch build assets."""

from __future__ import annotations

import importlib.resources as resources
from importlib.resources.abc import Traversable


def postgres_pgsearch_asset_root() -> Traversable:
    """Return the bundled postgres-pgsearch asset tree."""
    return resources.files("gobby").joinpath("data/postgres-pgsearch")
