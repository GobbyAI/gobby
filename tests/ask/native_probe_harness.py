#!/usr/bin/env python3
"""Seal observed fresh/resumed native Ask probe receipts into a pinned artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from gobby.ask.runtime_validation import (
    ask_runtime_control_digest,
    build_ask_runtime_probe_artifact,
    write_ask_runtime_probe_artifact,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the exact native Ask security matrix captured by an isolated "
            "managed fresh/resumed probe and write an immutable runtime artifact."
        )
    )
    parser.add_argument("--provider", default="claude")
    parser.add_argument("--provider-executable", type=Path, required=True)
    parser.add_argument("--auth-mode", default="claude.ai")
    parser.add_argument(
        "--observations",
        type=Path,
        required=True,
        help="JSON array of phase/case/observed/receipt objects from native launches",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    observations: Any = json.loads(arguments.observations.read_text(encoding="utf-8"))
    if not isinstance(observations, list):
        raise ValueError("native Ask observations must be a JSON array")
    control_digest = ask_runtime_control_digest(arguments.provider, arguments.auth_mode)
    artifact = build_ask_runtime_probe_artifact(
        provider=arguments.provider,
        provider_executable=arguments.provider_executable,
        auth_mode=arguments.auth_mode,
        control_digest=control_digest,
        observations=observations,
    )
    artifact_sha256 = write_ask_runtime_probe_artifact(arguments.output, artifact)
    print(
        json.dumps(
            {
                "artifact": str(arguments.output.resolve()),
                "sha256": artifact_sha256,
                "provider": artifact["provider"],
                "provider_executable": artifact["provider_executable"],
                "provider_version": artifact["provider_version"],
                "auth_mode": artifact["auth_mode"],
                "control_digest": artifact["control_digest"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
