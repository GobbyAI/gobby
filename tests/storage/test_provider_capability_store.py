"""Provider capability storage invariants against the applied baseline schema.

The standalone migration file this test used to replay was folded into the
embedded gdaemon baseline; the cascade contract now lives in
`crates/gcore/assets/schema/baseline.sql`
(`provider_model_routes_capability_fkey ... ON DELETE CASCADE`).
"""

from __future__ import annotations

import pytest

from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration


def test_route_rows_cascade_on_capability_delete(temp_db: HubDatabase) -> None:
    temp_db.execute(
        """
        INSERT INTO provider_model_capabilities (
            provider,
            canonical_model,
            display_name,
            generation,
            provenance
        ) VALUES ('openai', 'gpt-test', 'GPT Test', 1, '{}'::jsonb)
        """
    )
    temp_db.execute(
        """
        INSERT INTO provider_model_routes (
            provider,
            canonical_model,
            speed_mode,
            selector,
            generation,
            provenance
        ) VALUES ('openai', 'gpt-test', 'standard', 'gpt-test', 1, '{}'::jsonb)
        """
    )

    temp_db.execute(
        """
        DELETE FROM provider_model_capabilities
        WHERE provider = 'openai' AND canonical_model = 'gpt-test'
        """
    )

    route_count = temp_db.fetchone("SELECT COUNT(*) AS count FROM provider_model_routes")
    assert route_count is not None
    assert route_count["count"] == 0
