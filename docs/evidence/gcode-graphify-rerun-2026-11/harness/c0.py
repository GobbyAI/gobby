"""Run C0 for one owned attempt: probe Graphify, freeze the manifest, run it under SRT, write the receipt."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

from c0_poller import IndexPoller, clean_file_count, corpus_project_id, index_row_count
from c0_runner import (
    GCODE_CORPUS,
    GRAPHIFY_MARKER_PATTERN,
    PROBE_SYMBOLS,
    CommandResult,
    Runner,
    freeze_command_manifest,
    probe_graphify_concurrency,
    run_c0,
    run_process,
)
from launch_daemon import resolve_srt_runner, srt_policy, srt_wrapped_argv
from prepare_daemon import isolated_environment
from provision_environment import DEFAULT_RUNTIME_ROOT, _write_json
from runtime_boundary import assert_owned_runtime
from validate_environment import SEPTEMBER_ROOT

CLEAN_MANIFEST_RELATIVE = "manifests/corpora/gcode/C1.json"
# The expected gcode schema identity is not ruled yet, so C0 records that check as failed.
GCODE_SCHEMA_IDENTITY_PENDING: Any = None


def sandboxed_runner(root: Path, attempt: str) -> Runner:
    """Every C0 command runs under the SRT wrapper the other steps use, with this attempt's policy."""
    srt = resolve_srt_runner(root, attempt)
    policy = root / f"srt/c0-settings-{attempt}.json"
    violations = root / f"logs/c0/{attempt}/violations.jsonl"
    policy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    violations.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_json(policy, srt_policy(root, Path.home()), mode=0o600)
    violations.touch(mode=0o600, exist_ok=False)

    def runner(argv: list[str], cwd: Path, env: dict[str, str]) -> CommandResult:
        wrapped = srt_wrapped_argv(argv, policy=policy, violations=violations, srt=srt)
        return run_process(wrapped, cwd, env)

    return runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("attempt")
    parser.add_argument("--owner-session", required=True)
    args = parser.parse_args(argv)
    root = DEFAULT_RUNTIME_ROOT
    assert_owned_runtime(root, args.owner_session)
    ambient = dict(os.environ)
    runner = sandboxed_runner(root, args.attempt)
    concurrency = probe_graphify_concurrency(
        root,
        args.attempt,
        runner,
        september_root=SEPTEMBER_ROOT,
        env=isolated_environment(root, ambient),
    )
    freeze_command_manifest(root, probe_symbols=PROBE_SYMBOLS, graphify_concurrency=concurrency)
    (root / "receipts").mkdir(mode=0o700, parents=True, exist_ok=True)
    observer = IndexPoller(
        count=index_row_count(root, corpus_project_id(root, GCODE_CORPUS)),
        clock=time.monotonic,
        clean=clean_file_count(SEPTEMBER_ROOT / CLEAN_MANIFEST_RELATIVE),
        log=root / f"receipts/c7-poller-{args.attempt}.jsonl",
    )
    receipt = run_c0(
        root,
        args.attempt,
        runner,
        ambient=ambient,
        expected_schema_identity=GCODE_SCHEMA_IDENTITY_PENDING,
        observer=observer,
        graphify_marker_pattern=GRAPHIFY_MARKER_PATTERN,
    )
    state = "passed" if receipt["passed"] else "failed"
    print(f"C0 {args.attempt} {state}: {root / f'receipts/c0-{args.attempt}.json'}")
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
