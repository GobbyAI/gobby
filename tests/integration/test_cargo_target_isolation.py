"""Divergent checkouts of one project link to one project Cargo target.

Artifact provenance across sequential rebuilds is proven in
``tests/integration/test_project_cargo_cache.py``; this file pins the link layout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.agents.cargo_environment import project_cargo_target_dir
from gobby.agents.cargo_target import link_checkout_cargo_target

pytestmark = pytest.mark.integration


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


def test_divergent_checkouts_link_to_one_project_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    first = tmp_path / "checkout-main"
    second = tmp_path / "checkout-feature"
    _write_checkout(first, "main-output")
    _write_checkout(second, "feature-output")
    shared = project_cargo_target_dir("same-project", "gobby-target-probe")

    assert link_checkout_cargo_target(first, "same-project") is True
    assert link_checkout_cargo_target(second, "same-project") is True
    assert (first / "target").resolve() == shared.resolve()
    assert (second / "target").resolve() == shared.resolve()
