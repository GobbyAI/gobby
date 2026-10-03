"""Measure the retired #23117 bigram prefilter against landed bounded search (#23267).

Read-only: it generates a synthetic transcript in temporary state and runs each
arm in a fresh interpreter under its own temporary GOBBY_HOME; no daemon, no
database, no real transcript state.

``baseline`` drives the public ``search_session_messages`` boundary and follows
``next_cursor`` to exhaustion. ``prefilter`` signs every rendered group with the
prior branch's ``GroupSigner``, then serves each query by rendering only
candidate groups from one resolved snapshot under the same per-call group budget
and result limit, re-running the candidate scan on every call as that design did.

The prior module is not in the tree. Extract it first:

    git show e6f56851fc:src/gobby/sessions/transcript_search_index.py > prior.py
    uv run python tests/sessions/bench_transcript_search_prefilter.py --prior-module prior.py

Results and the materiality bar are recorded in
``.gobby/plans/research/transcript-search-bigram-prefilter-2026-10.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
SESSION_ID = "sess-23267"
LIMIT = 20
NEEDLE = "kumquat-needle-23267"
LOREM = "lorem ipsum dolor sit amet "

QUERIES = {
    "miss_distinct": "zzzz-no-such-token-anywhere",
    "miss_common_grams": "please check item 2000000",
    "rare_hit_common_grams": "please check item {last}",
    "rare_hit_distinct": NEEDLE,
    "common_hit": "lorem ipsum",
}


def build_fixture(path: Path, groups: int) -> None:
    """The #23117 mix shape: prompt, assistant text + tool_use, tool_result per turn.

    Each turn renders two groups (the prompt, and the assistant turn with its
    tool result), so ``groups`` rendered groups take ``groups // 2`` turns.
    """
    rng = random.Random(23117)
    turns = groups // 2
    needle_at = turns * 3 // 4
    with path.open("w", encoding="utf-8") as handle:
        for i in range(turns):
            ts = f"2026-09-01T00:00:{i % 60:02d}Z"
            text = LOREM * rng.randint(7, 107)
            if i == needle_at:
                text += NEEDLE
            tool_len = rng.randint(16000, 32000) if rng.random() < 0.15 else rng.randint(189, 3800)
            rows = [
                {
                    "type": "user",
                    "timestamp": ts,
                    "message": {"role": "user", "content": f"please check item {i}"},
                },
                {
                    "type": "assistant",
                    "timestamp": ts,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": text},
                            {
                                "type": "tool_use",
                                "id": f"toolu_{i}",
                                "name": "Bash",
                                "input": {"command": f"grep -rn item{i} src/"},
                            },
                        ],
                    },
                },
                {
                    "type": "user",
                    "timestamp": ts,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"toolu_{i}",
                                "content": (LOREM * (tool_len // len(LOREM) + 1))[:tool_len],
                            }
                        ],
                    },
                },
            ]
            for row in rows:
                handle.write(json.dumps(row) + "\n")


def _load_prior_module(path: str) -> ModuleType:
    """Load the prior signer over 0.5.0's search-text projection."""
    from gobby.sessions import transcript_search

    def rendered_message_search_text(message: Any) -> str:
        return transcript_search._message_search_text(message.to_dict())

    # The prior branch added this one-line projection to transcript_search; this
    # worker process exits after measuring, so the patch is never undone.
    patch.object(
        transcript_search, "rendered_message_search_text", rendered_message_search_text, create=True
    ).start()
    spec = importlib.util.spec_from_file_location("prior_search_index", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load prior module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _match_identities(results: list[dict[str, Any]]) -> list[str]:
    """Canonical identity of each matched message: its full rendered dict, id included."""
    return [json.dumps(match["message"], sort_keys=True) for match in results]


def _match_set_mismatches(reports: dict[str, dict[str, Any]]) -> list[str]:
    """Queries whose ordered match identities differ between the two arms."""
    baseline, prefilter = reports["baseline"]["queries"], reports["prefilter"]["queries"]
    return [
        name
        for name in QUERIES
        if (baseline[name]["matches"], baseline[name]["matches_sha256"])
        != (prefilter[name]["matches"], prefilter[name]["matches_sha256"])
    ]


def _summarize(calls: list[dict[str, Any]], found: list[str]) -> dict[str, Any]:
    return {
        "first_call_s": calls[0]["s"],
        "first_call_groups": calls[0]["groups"],
        "first_call_resolutions": calls[0]["resolutions"],
        "calls": len(calls),
        "max_call_s": max(call["s"] for call in calls),
        "total_s": sum(call["s"] for call in calls),
        "total_groups": sum(call["groups"] for call in calls),
        "total_resolutions": sum(call["resolutions"] for call in calls),
        "matches": len(found),
        "matches_sha256": hashlib.sha256("\0".join(found).encode()).hexdigest(),
    }


async def run_worker(arm: str, fixture: str, groups: int, prior_module: str) -> dict[str, Any]:
    from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
    from gobby.mcp_proxy.tools.sessions._messages import SEARCH_GROUP_BUDGET
    from gobby.sessions import transcript_reader as readers
    from gobby.sessions.transcript_index import clear_index_cache
    from gobby.sessions.transcript_reader import TranscriptReader, clear_archive_cache
    from gobby.sessions.transcript_search import search_rendered_messages
    from gobby.sessions.transcript_window import render_window

    def require_local_ownership(session: Any) -> str:
        session.machine_id = LOCAL_MACHINE_ID
        return LOCAL_MACHINE_ID

    patch.object(readers, "require_local_session_ownership", require_local_ownership).start()
    clear_archive_cache()
    clear_index_cache()

    rendered: list[int] = []
    resolutions: list[int] = []
    original_render = render_window
    original_resolve = TranscriptReader._resolve_windowable

    def counting_render(*args: Any, **kwargs: Any) -> Any:
        result = original_render(*args, **kwargs)
        rendered.append(result.returned_count)
        return result

    async def counting_resolve(self: TranscriptReader, *args: Any, **kwargs: Any) -> Any:
        resolutions.append(1)
        return await original_resolve(self, *args, **kwargs)

    patch.object(readers, "render_window", counting_render).start()
    patch.object(TranscriptReader, "_resolve_windowable", counting_resolve).start()

    session = MagicMock()
    session.external_id = "no-archive"
    session.source = "claude"
    session.transcript_path = fixture
    session.machine_id = LOCAL_MACHINE_ID
    manager = MagicMock()
    manager.get.return_value = session
    manager.resolve_session_reference.side_effect = lambda ref, _project=None: ref
    reader = TranscriptReader(manager)
    registry = create_session_messages_registry(session_manager=manager, transcript_reader=reader)

    def take() -> tuple[int, int]:
        counts = (sum(rendered), len(resolutions))
        rendered.clear()
        resolutions.clear()
        return counts

    started = time.perf_counter()
    resolved = await reader._open_windowable(SESSION_ID)
    report: dict[str, Any] = {
        "arm": arm,
        "index_build_s": time.perf_counter() - started,
        "queries": {},
    }
    if resolved is None or resolved.index is None:
        raise RuntimeError("fixture did not resolve to a windowable snapshot")
    take()

    if arm == "baseline":
        for name, template in QUERIES.items():
            query = template.format(last=groups // 2 - 1)
            calls: list[dict[str, Any]] = []
            found: list[str] = []
            cursor = None
            while True:
                args: dict[str, Any] = {"query": query, "session_id": SESSION_ID, "limit": LIMIT}
                if cursor:
                    args["cursor"] = cursor
                started = time.perf_counter()
                result = await registry.call("search_session_messages", args)
                elapsed = time.perf_counter() - started
                if not result.get("success"):
                    raise RuntimeError(result)
                rendered_groups, resolved_count = take()
                calls.append(
                    {"s": elapsed, "groups": rendered_groups, "resolutions": resolved_count}
                )
                found.extend(_match_identities(result.get("results", [])))
                cursor = result.get("next_cursor")
                if not cursor:
                    break
            report["queries"][name] = _summarize(calls, found)
        return report

    prior = _load_prior_module(prior_module)
    signer = prior.GroupSigner()
    sign_s = 0.0
    started = time.perf_counter()
    async for window in reader.iter_rendered_windows(SESSION_ID, order="head"):
        sign_started = time.perf_counter()
        for message in window.groups:
            signer.close_group(message)
        sign_s += time.perf_counter() - sign_started
    sign_pass_s = time.perf_counter() - started
    signatures = signer.snapshot(None, len(signer.widths))
    bitmap, widths = signatures.flatten()
    take()
    report["signing"] = {
        "groups_signed": len(widths),
        "admitted_width0": sum(1 for width in widths if width == 0),
        "bitmap_bytes": len(bitmap),
        "bitmap_cap_bytes": prior.MAX_SIGNATURE_BITS >> 3,
        "sign_cpu_s": sign_s,
        "sign_pass_wall_s": sign_pass_s,
    }

    for name, template in QUERIES.items():
        query = template.format(last=groups // 2 - 1)
        calls = []
        found = []
        position = 0
        candidates: list[int] = []
        scan_s = 0.0
        while not calls or position < len(candidates):
            started = time.perf_counter()
            candidates = signatures.candidate_group_indices(query)
            scan_s = scan_s or time.perf_counter() - started
            snapshot = await reader._open_windowable(SESSION_ID)
            budget = SEARCH_GROUP_BUDGET
            collected: list[dict[str, Any]] = []
            while position < len(candidates) and budget > 0 and len(collected) < LIMIT:
                run_start = candidates[position]
                run_len = 1
                while (
                    position + run_len < len(candidates)
                    and run_len < budget
                    and candidates[position + run_len] == run_start + run_len
                ):
                    run_len += 1
                window = await reader._render(
                    snapshot, SESSION_ID, run_len, run_start, order="head"
                )
                if not window.groups:
                    position += run_len
                    continue
                budget -= window.returned_count
                for message in window.groups:
                    position += 1
                    collected.extend(
                        search_rendered_messages(
                            session_id=SESSION_ID,
                            messages=[message],
                            query=query,
                            limit=LIMIT - len(collected),
                            full_content=True,
                        )
                    )
                    if len(collected) >= LIMIT:
                        break
            elapsed = time.perf_counter() - started
            rendered_groups, resolved_count = take()
            calls.append({"s": elapsed, "groups": rendered_groups, "resolutions": resolved_count})
            found.extend(_match_identities(collected))
        summary = _summarize(calls, found)
        summary["candidates"] = len(candidates)
        summary["candidate_scan_s"] = scan_s
        report["queries"][name] = summary
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups", type=int, default=200_000)
    parser.add_argument("--prior-module", required=True)
    parser.add_argument("--fixture")
    parser.add_argument("--worker", choices=["baseline", "prefilter"])
    args = parser.parse_args()

    if args.worker:
        report = asyncio.run(run_worker(args.worker, args.fixture, args.groups, args.prior_module))
        print("RESULT " + json.dumps(report), flush=True)
        return 0

    # Removes the generated fixture and arm homes; a caller-supplied --fixture is kept.
    with tempfile.TemporaryDirectory(prefix="gobby-23267-prefilter-") as tmp:
        return _compare_arms(args, Path(tmp))


def _compare_arms(args: argparse.Namespace, root: Path) -> int:
    fixture = Path(args.fixture) if args.fixture else root / "mix.jsonl"
    if not fixture.exists():
        build_fixture(fixture, args.groups)
    print(f"groups={args.groups} fixture={fixture} bytes={fixture.stat().st_size}", flush=True)
    reports: dict[str, dict[str, Any]] = {}
    for arm in ("baseline", "prefilter"):
        env = dict(os.environ, GOBBY_HOME=str(root / f"home-{arm}"))
        proc = subprocess.run(
            [
                sys.executable,
                __file__,
                "--worker",
                arm,
                "--fixture",
                str(fixture),
                "--groups",
                str(args.groups),
                "--prior-module",
                str(Path(args.prior_module).resolve()),
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        if proc.returncode != 0:
            print(f"{arm} failed rc={proc.returncode}\n{proc.stderr[-4000:]}", flush=True)
            return 1
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")][-1]
        reports[arm] = json.loads(line[len("RESULT ") :])
        print(json.dumps(reports[arm], indent=1), flush=True)
    mismatches = _match_set_mismatches(reports)
    if mismatches:
        print(f"match sets differ for: {', '.join(mismatches)}", flush=True)
        return 1
    print(f"match sets identical for all {len(QUERIES)} queries", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
