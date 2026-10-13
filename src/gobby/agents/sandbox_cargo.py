"""Materialize a Cargo launcher within a sandbox's read-only run assets."""

from __future__ import annotations

import os
import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path


def sandbox_cargo_environment(
    *, assets: Path, node: Path, workspace: Path, target: Path, env: Mapping[str, str]
) -> dict[str, str]:
    """Route cross-checkout builds below the existing writable target grant."""
    # Resume may carry the previous run's PATH. Never chain its read-only shim,
    # whose assets belong to another run and are outside this run's read grant.
    search_path = os.pathsep.join(
        entry
        for entry in env.get("PATH", "").split(os.pathsep)
        if not (Path(entry).name == "cargo-bin" and Path(entry).parent.name == "assets")
    )
    cargo = shutil.which("cargo", path=search_path)
    if cargo is None:
        return {}
    runner = assets / "cargo-runner.mjs"
    runner.unlink(missing_ok=True)
    shutil.copyfile(Path(__file__).with_name("sandbox_cargo_runner.mjs"), runner)
    runner.chmod(0o400)
    directory = assets / "cargo-bin"
    directory.mkdir(mode=0o700, exist_ok=True)
    launcher = directory / "cargo"
    launcher.unlink(missing_ok=True)
    arguments = [str(node), str(runner), cargo, str(workspace.resolve()), str(target)]
    launcher.write_text(f'#!/bin/sh\nexec {shlex.join(arguments)} "$@"\n', encoding="utf-8")
    launcher.chmod(0o500)
    return {
        "PATH": f"{directory}{os.pathsep}{search_path}",
        "GOBBY_SANDBOX_CARGO_WORKSPACE": str(workspace.resolve()),
        "GOBBY_SANDBOX_CARGO_TARGET": str(target),
    }
