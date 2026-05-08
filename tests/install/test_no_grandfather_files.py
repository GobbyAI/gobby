from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SHARED = PROJECT_ROOT / "src" / "gobby" / "install" / "shared"
RETIRED_PATTERNS = (
    ".grand" + "fathered*",
    ".legacy" + "-classification.yaml",
)


def test_no_grandfather_files_under_install() -> None:
    offenders = sorted(
        str(path.relative_to(PROJECT_ROOT))
        for pattern in RETIRED_PATTERNS
        for path in INSTALL_SHARED.rglob(pattern)
    )

    assert not offenders, "retired install classification files exist: " + ", ".join(offenders)
