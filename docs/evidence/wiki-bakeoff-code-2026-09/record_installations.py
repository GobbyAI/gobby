"""Record immutable, byte-verified observations of the installed pinned tools."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prepare_daemon import isolated_environment
from provision_environment import DEFAULT_RUNTIME_ROOT, GOBBY_SHA
from runtime_boundary import assert_owned_runtime, file_record, verify_file_record, write_once
from validate_environment import PINS, validate_installations

ENTRYPOINTS = {
    "graphify": ["tools/graphify/venv/bin/graphify"],
    "understand-anything": [
        "sources/understand-anything/understand-anything-plugin/packages/core/dist/index.js",
        "sources/understand-anything/understand-anything-plugin/skills/understand/SKILL.md",
    ],
    "archify": ["sources/archify/archify/SKILL.md", "sources/archify/archify/bin/archify.mjs"],
    "codewiki": ["tools/codewiki/venv/bin/codewiki"],
    "opendeepwiki": [
        "tools/opendeepwiki/publish/OpenDeepWiki.dll",
        "sources/opendeepwiki/web/.next/standalone/server.js",
    ],
    "grok-wiki": [
        "tools/grok-wiki/Grok-Wiki.app/Contents/MacOS/grok-wiki-desktop",
        "tools/grok-wiki/Grok-Wiki.app/Contents/Resources/bun/bun",
        "tools/grok-wiki/Grok-Wiki.app/Contents/Resources/server/rlm-wiki.js",
        "tools/grok-wiki/Grok-Wiki.app/Contents/Info.plist",
    ],
}
LOCKS = {
    "graphify": ["sources/graphify/uv.lock"],
    "understand-anything": ["sources/understand-anything/pnpm-lock.yaml"],
    "archify": ["sources/archify/archify/package-lock.json"],
    "codewiki": ["build/codewiki-requirements.txt"],
    "opendeepwiki": ["sources/opendeepwiki/web/bun.lock"],
    "grok-wiki": [],
}


def probe(root: Path, attempt: str, label: str, argv: list[str], cwd: Path) -> dict[str, Any]:
    receipt = root / f"receipts/probe-{label}-{attempt}.json"
    if receipt.exists():
        check = json.loads(receipt.read_text())
        assert check["argv"] == argv and check["cwd"] == str(cwd)
        verify_file_record(root, check["log"])
        assert check["exit_code"] == 0, (
            "failed attempt preserved; use a new attempt after diagnosis"
        )
        return dict(check)
    log = root / f"logs/install/probe-{label}-{attempt}.log"
    started = datetime.now(UTC).isoformat()
    with log.open("x") as output:
        os.chmod(log, 0o600)
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=isolated_environment(root, dict(os.environ)),
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
    assert result.returncode == 0, f"{label} failed; see {log}"
    print(f"{label}: passed", flush=True)
    return check


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-session", required=True)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    assert args.attempt.isalnum(), "attempt must be alphanumeric"
    root = DEFAULT_RUNTIME_ROOT
    assert_owned_runtime(root, args.owner_session)
    node, uv = shutil.which("node"), shutil.which("uv")
    assert node and uv
    checks: dict[str, list[dict[str, Any]]] = {name: [] for name in PINS}
    for name in ("graphify", "codewiki"):
        checks[name].append(
            probe(
                root,
                args.attempt,
                f"{name}-version",
                [str(root / ENTRYPOINTS[name][0]), "--version"],
                root,
            )
        )
        checks[name].append(
            probe(
                root,
                args.attempt,
                f"{name}-dependencies",
                [uv, "pip", "check", "--python", str(root / f"tools/{name}/venv/bin/python")],
                root,
            )
        )
    archify = root / "sources/archify/archify"
    checks["archify"].append(
        probe(
            root,
            args.attempt,
            "archify-validators",
            [node, "scripts/generate-validators.mjs", "--check"],
            archify,
        )
    )
    core = root / "sources/understand-anything/understand-anything-plugin/packages/core"
    checks["understand-anything"].append(
        probe(
            root,
            args.attempt,
            "understand-anything-import",
            [
                node,
                "--input-type=module",
                "-e",
                "await import('./dist/index.js'); console.log('native core import passed')",
            ],
            core,
        )
    )
    resources = root / "tools/grok-wiki/Grok-Wiki.app/Contents/Resources"
    checks["grok-wiki"].append(
        probe(
            root,
            args.attempt,
            "grok-wiki-signature",
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(resources.parent.parent)],
            root,
        )
    )
    for name, url in (
        ("opendeepwiki", "http://127.0.0.1:61239/health"),
        ("grok-wiki", "http://127.0.0.1:61241/api/health"),
    ):
        checks[name].append(
            probe(
                root,
                args.attempt,
                f"{name}-health",
                ["/usr/bin/curl", "--fail", "--silent", "--show-error", "--max-time", "10", url],
                root,
            )
        )
    comparators: dict[str, Any] = {}
    for name, pin in PINS.items():
        locks = list(LOCKS[name])
        if name == "opendeepwiki":
            for directory in ("src", "framework"):
                locks.extend(
                    str(path.relative_to(root))
                    for path in sorted(
                        (root / "sources/opendeepwiki" / directory).rglob("packages.lock.json")
                    )
                )
        item: dict[str, Any] = {
            "required_pin": pin,
            "status": "installed",
            "entrypoints": [file_record(root, path) for path in ENTRYPOINTS[name]],
            "dependency_locks": [file_record(root, path) for path in locks],
            "checks": checks[name],
            "evidence": [],
        }
        if name == "grok-wiki":
            item["release_artifact"] = file_record(root, "sources/Grok-Wiki_0.0.38_aarch64.dmg")
            item["dependency_lock_status"] = "dependencies embedded in signed release"
        else:
            source_path = root / f"receipts/{name}-source.json"
            source = json.loads(source_path.read_text())
            item["source_receipt"] = file_record(root, source_path)
            item["source_manifest"] = file_record(root, f"manifests/{name}-source.json")
            item["source_archive"] = file_record(root, source["archive"])
        if name == "graphify":
            item["version_log"] = checks[name][0]["log"]
        if name == "understand-anything":
            item["evidence"].extend(
                file_record(root, path)
                for path in (
                    "receipts/understand-anything-parser-smoke.json",
                    "logs/install/understand-anything-parser-smoke-12124.log",
                    "receipts/project-local-skills.json",
                )
            )
        comparators[name] = item
    binary = root / "tools/gcode/bin/gcode"
    version = probe(root, args.attempt, "gcode-version", [str(binary), "--version"], root)
    contract = probe(root, args.attempt, "gcode-contract", [str(binary), "contract"], root)
    gcode = {
        "source_commit": GOBBY_SHA,
        "version": "1.7.0",
        "contract_version": 8,
        "executable": file_record(root, binary),
        "gdaemon": file_record(root, "gobby-home/bin/gdaemon"),
        "cargo_lock": file_record(root, "sources/gobby/Cargo.lock"),
        "source_manifest": file_record(root, "manifests/gobby-source.json"),
        "checks": [version, contract],
        "version_log": version["log"],
        "contract_log": contract["log"],
    }
    write_once(root / "receipts/gcode.json", json.dumps(gcode, indent=2) + "\n", 0o600)
    write_once(
        root / "receipts/installations.json",
        json.dumps({"comparators": comparators}, indent=2) + "\n",
        0o600,
    )
    validate_installations(root)
    print("all pinned installation artifacts verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
