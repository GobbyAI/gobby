from __future__ import annotations

import asyncio

from gobby.projects.write_fence import ProjectWriteFence

TEST_WAIT_TIMEOUT_SECONDS = 2.0


async def wait_for_exclusive_claim(fence: ProjectWriteFence, project_id: str) -> None:
    """Wait until a project fence records exclusive ownership."""

    async def wait() -> None:
        async with fence._condition:
            await fence._condition.wait_for(lambda: project_id in fence._exclusive)

    await asyncio.wait_for(wait(), timeout=TEST_WAIT_TIMEOUT_SECONDS)
