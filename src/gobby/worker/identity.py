"""The release-pinned schema identity, read once per process.

A handshake answers with this identity. The packaged file is read on the first call and
the result is kept. A read that raises is not kept, so the next call reads again.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping

from gobby.storage.schema_contract import expected_schema_identity


@functools.cache
def _cached_identity() -> Mapping[str, int | str]:
    return expected_schema_identity()


def schema_identity() -> dict[str, int | str]:
    """Return a copy of the release-pinned schema identity."""
    return dict(_cached_identity())
