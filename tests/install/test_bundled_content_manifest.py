from __future__ import annotations

import json
from pathlib import Path

from gobby.install.manifest import build_bundled_content_manifest


def test_bundled_content_manifest_matches_tree() -> None:
    install_dir = Path(__file__).resolve().parents[2] / "src" / "gobby" / "install"
    committed = json.loads(
        (install_dir / "bundled_content_manifest.json").read_text(encoding="utf-8")
    )

    assert committed == build_bundled_content_manifest(install_dir / "shared")
