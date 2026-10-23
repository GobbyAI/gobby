"""Real Cargo proof that divergent checkouts cannot exchange artifacts."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - fixed Cargo argv in an isolated fixture
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gobby.agents.cargo_target import (
    checkout_cargo_target_dir,
    link_checkout_cargo_target,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed"),
]


def _write_checkout(checkout: Path, output: str) -> None:
    (checkout / "src").mkdir(parents=True)
    (checkout / "Cargo.toml").write_text(
        '[package]\nname = "gobby-target-probe"\nversion = "0.1.0"\nedition = "2024"\n',
        encoding="utf-8",
    )
    (checkout / "src" / "main.rs").write_text(
        f'fn main() {{ println!("{output}"); }}\n',
        encoding="utf-8",
    )


def _build(checkout: Path, *, cargo_home: Path, target: Path) -> None:
    env = {
        **os.environ,
        "CARGO_HOME": str(cargo_home),
        "CARGO_TARGET_DIR": str(target),
        "CARGO_NET_OFFLINE": "true",
    }
    subprocess.run(  # nosec B603 - fixed Cargo argv and isolated fixture paths
        ["cargo", "build", "--quiet"],
        cwd=checkout,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_concurrent_divergent_checkouts_build_branch_correct_binaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    first = tmp_path / "checkout-main"
    second = tmp_path / "checkout-feature"
    _write_checkout(first, "main-output")
    _write_checkout(second, "feature-output")
    project_id = "same-project"
    first_target = checkout_cargo_target_dir(first, project_id)
    second_target = checkout_cargo_target_dir(second, project_id)
    cargo_home = tmp_path / "gobby-home" / "cache" / "cargo-home"
    cargo_home.mkdir(parents=True)

    assert first_target != second_target
    assert link_checkout_cargo_target(first, project_id) is True
    assert link_checkout_cargo_target(second, project_id) is True

    with ThreadPoolExecutor(max_workers=2) as pool:
        builds = [
            pool.submit(_build, first, cargo_home=cargo_home, target=first_target),
            pool.submit(_build, second, cargo_home=cargo_home, target=second_target),
        ]
        for build in builds:
            build.result()

    suffix = ".exe" if os.name == "nt" else ""
    first_binary = first_target / "debug" / f"gobby-target-probe{suffix}"
    second_binary = second_target / "debug" / f"gobby-target-probe{suffix}"
    assert (
        subprocess.run(  # nosec B603 - fixture-owned executable
            [str(first_binary)], check=True, capture_output=True, text=True, timeout=10
        ).stdout.strip()
        == "main-output"
    )
    assert (
        subprocess.run(  # nosec B603 - fixture-owned executable
            [str(second_binary)], check=True, capture_output=True, text=True, timeout=10
        ).stdout.strip()
        == "feature-output"
    )
