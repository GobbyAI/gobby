"""HTTP response helpers for server JSON boundaries."""

from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse as StarletteJSONResponse

from gobby.utils.datetime import to_json_safe


class JSONResponse(StarletteJSONResponse):
    """JSONResponse that serializes datetime/date values at the HTTP boundary."""

    def render(self, content: Any) -> bytes:
        return super().render(to_json_safe(content))
