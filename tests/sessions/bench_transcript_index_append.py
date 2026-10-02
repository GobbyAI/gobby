"""Calibrate cold replay and resident append on a retained synthetic fixture.

Run with uv run and two arguments: synthetic JSONL and its complete sidecar.
This macOS harness clones the fixture into temporary state and mutates only that
clone. It accepts an existing full index so cache-loading timing excludes setup.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._messages import register_message_tools
from gobby.sessions import transcript_index as indexes
from gobby.sessions import transcript_index_sidecar as sidecars
from gobby.sessions import transcript_reader as readers
from gobby.sessions.transcript_window import WindowResult, render_window


async def main() -> None:
    fixture, seed = (Path(value) for value in sys.argv[1:])
    root = Path(tempfile.mkdtemp(prefix="gobby-23117-append-bench-"))
    os.environ["GOBBY_HOME"] = str(root / "home")
    path = root / "mix.jsonl"
    subprocess.run(["cp", "-c", str(fixture), str(path)], check=True)
    payload = json.loads(seed.read_text())
    st = path.stat()
    if payload["size"] != st.st_size:
        raise ValueError("seed sidecar must cover the entire synthetic fixture")
    session_id = payload["session_id"]
    index = sidecars._payload_to_index(payload)
    index.mtime_ns = st.st_mtime_ns
    sidecars.persist_index_sidecar(str(path), index)
    del index, payload

    manager = MagicMock()
    manager.resolve_session_reference.side_effect = lambda ref, _project=None: ref
    session = SimpleNamespace(
        id=session_id,
        external_id="synthetic",
        source="claude",
        transcript_path=str(path),
        machine_id="synthetic-local",
    )
    manager.get.side_effect = {session_id: session}.get
    registry = InternalToolRegistry(name="gobby-sessions", description="isolated append benchmark")
    register_message_tools(registry, manager, readers.TranscriptReader(manager))
    base = Path(sidecars._sidecar_path(str(path)))
    base_stat = base.stat()
    rows: list[dict[str, object]] = []

    async def measure(label: str) -> None:
        groups = 0
        original_render = render_window

        def counted_render(*args: Any, **kwargs: Any) -> WindowResult:
            nonlocal groups
            result = original_render(*args, **kwargs)
            groups += result.returned_count
            return result

        with (
            patch.object(
                readers, "require_local_session_ownership", return_value="synthetic-local"
            ),
            patch.object(sidecars, "load_index_sidecar", wraps=sidecars.load_index_sidecar) as load,
            patch.object(
                indexes, "build_index_from_file", wraps=indexes.build_index_from_file
            ) as build,
            patch.object(
                sidecars, "_source_prefix_sha256", wraps=sidecars._source_prefix_sha256
            ) as full_hash,
            patch.object(readers, "render_window", side_effect=counted_render),
            patch.object(
                readers.TranscriptReader,
                "_resolve_windowable",
                autospec=True,
                side_effect=readers.TranscriptReader._resolve_windowable,
            ) as resolve,
        ):
            started = time.perf_counter()
            result = await registry.call(
                "search_session_messages", {"query": "absent needle", "session_id": session_id}
            )
            elapsed = time.perf_counter() - started
        if not result.get("success") or not result.get("truncated"):
            raise AssertionError(result)
        if groups != 2000 or resolve.call_count != 1 or build.call_count != 0:
            raise AssertionError("search exceeded its calibrated work contract")
        if label == "resident_append" and (load.call_count or full_hash.call_count or elapsed > 1):
            raise AssertionError(f"resident append contract failed: {elapsed:.6f}s")
        rows.append(
            {
                "phase": label,
                "seconds": elapsed,
                "sidecar_loads": load.call_count,
                "full_builds": build.call_count,
                "full_prefix_hashes": full_hash.call_count,
                "groups": groups,
                "resolutions": resolve.call_count,
                "journal_bytes": Path(str(base) + ".journal").stat().st_size,
            }
        )

    indexes.clear_index_cache()
    await measure("cold_sidecar_load")
    await measure("resident_warm")
    with path.open("a") as handle:
        handle.write(
            json.dumps({"type": "user", "message": {"role": "user", "content": "x"}}) + "\n"
        )
    await measure("resident_append")
    if base.stat().st_mtime_ns != base_stat.st_mtime_ns or base.stat().st_size != base_stat.st_size:
        raise AssertionError("append rewrote the base sidecar")
    print(json.dumps({"root": str(root), "fixture_bytes": st.st_size, "rows": rows}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
