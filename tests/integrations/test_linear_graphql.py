from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gobby.integrations import linear_graphql
from gobby.integrations.linear_graphql import LinearGraphQLClient, LinearGraphQLError


@pytest.mark.asyncio
async def test_execute_retry_error_uses_attempt_constant() -> None:
    client = LinearGraphQLClient("lin_api_key")
    async_client = AsyncMock()
    async_client.post.side_effect = httpx.TimeoutException("timed out")
    async_client.__aenter__.return_value = async_client
    async_client.__aexit__.return_value = False

    with (
        patch("gobby.integrations.linear_graphql.httpx.AsyncClient", return_value=async_client),
        patch("gobby.integrations.linear_graphql.asyncio.sleep", new=AsyncMock()),
    ):
        with pytest.raises(LinearGraphQLError) as exc_info:
            await client.execute("query Test { viewer { id } }")

    assert f"{linear_graphql._MAX_ATTEMPTS} attempts" in str(exc_info.value)
    assert async_client.post.await_count == linear_graphql._MAX_ATTEMPTS
