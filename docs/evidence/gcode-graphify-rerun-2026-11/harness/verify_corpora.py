"""Read-only check that the frozen corpus cases match their recorded manifests.

Each case is compared with its manifest by type, mode, size, and SHA-256. A recorded file that is
missing or changed fails the check. A file the manifest does not record is reported as extra.
Nothing is written under the runtime root.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from provision_environment import COMPARATOR_CASES, DEFAULT_RUNTIME_ROOT, build_manifest
from runtime_boundary import require

COMPARED_FIELDS = ("type", "mode", "bytes", "sha256")


def compare_case(case_root: Path, manifest: Mapping[str, Any]) -> dict[str, list[str]]:
    recorded = {item["path"]: item for item in manifest["files"]}
    actual_manifest = build_manifest(
        case_root, manifest["source_commit"], manifest["source_exclusions"]
    )
    actual = {item["path"]: item for item in actual_manifest["files"]}
    changed = [
        path
        for path in sorted(recorded.keys() & actual.keys())
        if any(recorded[path][field] != actual[path][field] for field in COMPARED_FIELDS)
    ]
    return {
        "missing": sorted(recorded.keys() - actual.keys()),
        "changed": changed,
        "extra": sorted(actual.keys() - recorded.keys()),
    }


def _cases(cases: Mapping[str, tuple[str, ...]] | None) -> Mapping[str, tuple[str, ...]]:
    return COMPARATOR_CASES if cases is None else cases


def compare_manifests(
    root: Path,
    september_root: Path,
    cases: Mapping[str, tuple[str, ...]] | None = None,
) -> None:
    for comparator, names in _cases(cases).items():
        for case in names:
            relative = Path("manifests/corpora") / comparator / f"{case}.json"
            rerun = (root / relative).read_bytes()
            september = (september_root / relative).read_bytes()
            require(rerun == september, f"manifest differs from September: {comparator}/{case}")


def verify_corpora(
    root: Path, cases: Mapping[str, tuple[str, ...]] | None = None
) -> dict[str, dict[str, list[str]]]:
    report: dict[str, dict[str, list[str]]] = {}
    for comparator, names in _cases(cases).items():
        for case in names:
            manifest_path = root / "manifests/corpora" / comparator / f"{case}.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            report[f"{comparator}/{case}"] = compare_case(
                root / "corpora" / comparator / case, manifest
            )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--september-root", type=Path, default=None)
    args = parser.parse_args()
    root = args.runtime_root.resolve()
    if args.september_root is not None:
        compare_manifests(root, args.september_root.resolve())
    report = verify_corpora(root)
    print(json.dumps(report, indent=2, sort_keys=True))
    failed = any(item["missing"] or item["changed"] for item in report.values())
    print("corpora match their manifests" if not failed else "corpora differ from manifests")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
