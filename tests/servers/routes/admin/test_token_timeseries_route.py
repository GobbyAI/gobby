"""Consistency tests for the /tokens/timeseries granularity validator.

The FastAPI route uses a string-literal regex (``pattern="^(30m|1h|1d)$"``) for
query validation, while ``gobby.storage.token_events`` declares the canonical
set in ``VALID_GRANULARITIES`` and ``TimeSeriesGranularity``. These can drift.
This module pins them together: every member of ``VALID_GRANULARITIES`` must
be accepted by the route's regex, and obvious non-members must be rejected.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from gobby.servers.routes.admin._token_timeseries import register_token_timeseries_routes
from gobby.storage.token_events import VALID_GRANULARITIES

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def granularity_pattern() -> str:
    app = FastAPI()
    router = APIRouter()
    server_mock = MagicMock()
    server_mock.services.database = MagicMock()
    register_token_timeseries_routes(router, server_mock)
    app.include_router(router)

    route = None
    for candidate in app.routes:
        if isinstance(candidate, APIRoute) and candidate.path.endswith("/tokens/timeseries"):
            route = candidate
            break
    if route is None:
        raise AssertionError("tokens timeseries route was not registered")

    granularity_param = None
    for candidate in route.dependant.query_params:
        if candidate.name == "granularity":
            granularity_param = candidate
            break
    if granularity_param is None:
        raise AssertionError("tokens timeseries route is missing `granularity` query param")
    metadata = getattr(granularity_param.field_info, "metadata", []) or []
    for entry in metadata:
        pattern = getattr(entry, "pattern", None)
        if isinstance(pattern, str):
            return pattern
    raise AssertionError("granularity Query is missing a string `pattern` constraint")


def test_route_pattern_accepts_every_valid_granularity(granularity_pattern: str) -> None:
    pattern = re.compile(granularity_pattern)
    for value in VALID_GRANULARITIES:
        assert pattern.fullmatch(value), (
            f"VALID_GRANULARITIES member {value!r} not accepted by route pattern "
            f"{pattern.pattern!r}"
        )


@pytest.mark.parametrize("value", ["", "5m", "1h ", " 1h", "1H", "2h", "1d1h"])
def test_route_pattern_rejects_non_members(value: str, granularity_pattern: str) -> None:
    pattern = re.compile(granularity_pattern)
    assert not pattern.fullmatch(value), (
        f"route pattern {pattern.pattern!r} unexpectedly accepted non-member {value!r}"
    )


@pytest.mark.parametrize(
    "candidate",
    list(VALID_GRANULARITIES) + ["", "5m", "2h", "1H", "1d1h", "30s", "1y"],
)
def test_route_pattern_only_matches_members(candidate: str, granularity_pattern: str) -> None:
    pattern = re.compile(granularity_pattern)
    if pattern.fullmatch(candidate):
        assert candidate in VALID_GRANULARITIES, (
            f"route pattern {pattern.pattern!r} accepted non-member "
            f"{candidate!r}; if intended, add it to VALID_GRANULARITIES."
        )
