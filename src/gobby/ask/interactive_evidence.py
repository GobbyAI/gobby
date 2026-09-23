"""Direct, bounded native evidence retrieval for authenticated interactive callers."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any, Literal


async def retrieve_evidence(
    *,
    executable: Path,
    project_root: Path,
    operation: Literal["search", "read", "graph", "communities"],
    selector: dict[str, Any],
    continuation: str | None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "schema_version": 1,
        "operation": operation,
        operation: selector,
    }
    if continuation is not None:
        request["continuation"] = continuation
    process = await asyncio.create_subprocess_exec(
        str(executable),
        "--quiet",
        "--format",
        "json",
        "--project",
        str(project_root),
        "evidence",
        "--request-json",
        json.dumps(request),
        cwd=project_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(30):
            stdout, stderr = await process.communicate()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(f"gcode evidence failed: {stderr.decode(errors='replace')}")
    response = json.loads(stdout)
    if not isinstance(response, dict):
        raise RuntimeError("gcode evidence returned a non-object response")
    return response
