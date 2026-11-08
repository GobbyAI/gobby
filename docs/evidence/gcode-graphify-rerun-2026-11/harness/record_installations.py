"""Record immutable, byte-verified observations of the installed Graphify and gcode tools.

Graphify is installed from PyPI and recorded by its frozen version and lock. gcode is recorded
from the installed binary set. Every receipt is validated in memory before any is written, so a
failed validation leaves no receipt behind.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prepare_daemon import isolated_environment
from provision_environment import DEFAULT_RUNTIME_ROOT, GOBBY_SHA
from runtime_boundary import (
    EvidenceError,
    assert_owned_runtime,
    file_record,
    require,
    verify_file_record,
    write_once,
)
from schema_head import gcode_binary_relative, read_archived_gcode_version
from validate_environment import (
    GCODE_CONTRACT_VERSION,
    GRAPHIFY_ENTRYPOINT,
    validate_gcode_receipt,
    validate_graphify_receipt,
)


def probe(
    root: Path,
    attempt: str,
    label: str,
    argv: list[str],
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    receipt = root / f"receipts/probe-{label}-{attempt}.json"
    if receipt.exists():
        check = json.loads(receipt.read_text())
        require(
            check["argv"] == argv and check["cwd"] == str(cwd),
            "check failed: check['argv'] == argv and check['cwd'] == str(cwd)",
        )
        verify_file_record(root, check["log"])
        require(
            check["exit_code"] == 0, "failed attempt preserved; use a new attempt after diagnosis"
        )
        return dict(check)
    log = root / f"logs/install/probe-{label}-{attempt}.log"
    started = datetime.now(UTC).isoformat()
    with log.open("x") as output:
        os.chmod(log, 0o600)
        result = subprocess.run(
            argv,
            cwd=cwd,
            env={**isolated_environment(root, dict(os.environ)), **(env or {})},
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
    check = {
        "argv": argv,
        "cwd": str(cwd),
        "started_at": started,
        "ended_at": datetime.now(UTC).isoformat(),
        "exit_code": result.returncode,
        "log": file_record(root, log),
    }
    write_once(receipt, json.dumps(check, indent=2) + "\n", 0o600)
    require(result.returncode == 0, f"{label} failed; see {log}")
    print(f"{label}: passed", flush=True)
    return check


def _pypi_version(root: Path, freeze_log: dict[str, Any]) -> str:
    for line in verify_file_record(root, freeze_log).read_text().splitlines():
        if line.lower().startswith("graphifyy=="):
            return line.split("==", 1)[1].strip()
    raise EvidenceError("graphifyy is absent from the frozen Graphify environment")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-session", required=True)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    require(args.attempt.isalnum(), "attempt must be alphanumeric")
    root = DEFAULT_RUNTIME_ROOT
    assert_owned_runtime(root, args.owner_session)
    uv = shutil.which("uv")
    require(uv, "check failed: uv")
    graphify_python = str(root / "tools/graphify/venv/bin/python")
    version = probe(
        root,
        args.attempt,
        "graphify-version",
        [str(root / GRAPHIFY_ENTRYPOINT), "--version"],
        root,
    )
    dependencies = probe(
        root,
        args.attempt,
        "graphify-dependencies",
        [uv, "pip", "check", "--python", graphify_python],
        root,
    )
    freeze = probe(
        root,
        args.attempt,
        "graphify-freeze",
        [uv, "pip", "freeze", "--python", graphify_python],
        root,
    )
    graphify = {
        "status": "installed",
        "pypi_package": "graphifyy",
        "pypi_version": _pypi_version(root, freeze["log"]),
        "entrypoints": [file_record(root, GRAPHIFY_ENTRYPOINT)],
        "dependency_locks": [freeze["log"]],
        "checks": [version, dependencies],
        "version_log": version["log"],
        "evidence": [],
    }
    binary = root / gcode_binary_relative()
    gcode_version = probe(root, args.attempt, "gcode-version", [str(binary), "--version"], root)
    contract = probe(root, args.attempt, "gcode-contract", [str(binary), "contract"], root)
    gcode = {
        "source_commit": GOBBY_SHA,
        "version": read_archived_gcode_version(root),
        "contract_version": GCODE_CONTRACT_VERSION,
        "executable": file_record(root, binary),
        "gdaemon": file_record(root, "gobby-home/bin/gdaemon"),
        "cargo_lock": file_record(root, "sources/gobby/Cargo.lock"),
        "source_manifest": file_record(root, "manifests/gobby-source.json"),
        "checks": [gcode_version, contract],
        "version_log": gcode_version["log"],
        "contract_log": contract["log"],
    }
    # Validate the in-memory receipts before writing either one: a failure writes nothing.
    validate_graphify_receipt(root, graphify)
    validate_gcode_receipt(root, gcode)
    write_once(root / "receipts/gcode.json", json.dumps(gcode, indent=2) + "\n", 0o600)
    write_once(
        root / "receipts/installations.json",
        json.dumps({"comparators": {"graphify": graphify}}, indent=2) + "\n",
        0o600,
    )
    print("all pinned installation artifacts verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
