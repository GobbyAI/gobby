"""Release packaging assertions for gobby-terminal and gobby-client."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("GOBBY_RUN_VENDOR_BUILD") != "1",
        reason="set GOBBY_RUN_VENDOR_BUILD=1 to run vendor packaging checks",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-gterminal.yml"
PACKAGE_ASSERTION_STEP = "Package gobby-terminal and assert provenance inputs"


def _release_assertion_paths() -> list[str]:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test"]["steps"]
    script = next(step["run"] for step in steps if step.get("name") == PACKAGE_ASSERTION_STEP)
    match = re.search(r"for required in \\\n(?P<paths>.*?)\n\s*do\b", script, re.DOTALL)
    assert match is not None, f"{PACKAGE_ASSERTION_STEP!r} has no required-path loop"
    return [line.strip().removesuffix("\\").rstrip() for line in match["paths"].splitlines()]


def _cargo_package(package: str, *options: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["cargo", "package", "-p", package, *options, "--no-verify"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_assertion_paths_are_packaged() -> None:
    result = _cargo_package("gobby-terminal", "--list")

    assert result.returncode == 0, result.stdout + result.stderr
    packaged_paths = set(result.stdout.splitlines())
    missing = [path for path in _release_assertion_paths() if path not in packaged_paths]
    assert missing == []


@pytest.mark.parametrize("package", ["gobby-terminal", "gobby-client"])
def test_package_emits_no_license_warning(package: str) -> None:
    # gobby-client can stop on unpublished dependencies; this contract isolates license diagnostics.
    result = _cargo_package(package)

    license_warnings = [
        line
        for line in (result.stdout + result.stderr).splitlines()
        if "warning" in line.lower() and re.search(r"\blicen[cs]e\b", line, re.IGNORECASE)
    ]
    assert license_warnings == [], f"{package}: {license_warnings}"
