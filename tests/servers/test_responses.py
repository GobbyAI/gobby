"""Tests for HTTP response boundary serialization."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gobby.servers.responses import JSONResponse

pytestmark = pytest.mark.unit


def test_json_response_serializes_datetime_content() -> None:
    response = JSONResponse(
        content={
            "created_at": datetime(2026, 7, 3, 12, 34, tzinfo=UTC),
            "items": [{"updated_at": datetime(2026, 7, 3, 12, 35, tzinfo=UTC)}],
        }
    )

    assert json.loads(response.body) == {
        "created_at": "2026-07-03T12:34:00+00:00",
        "items": [{"updated_at": "2026-07-03T12:35:00+00:00"}],
    }
