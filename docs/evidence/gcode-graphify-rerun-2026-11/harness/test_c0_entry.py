"""c0.py refuses any runtime that the owned session does not hold before it runs a command."""

from __future__ import annotations

import json
from pathlib import Path

import c0
import pytest
from runtime_boundary import EvidenceError


def test_c0_refuses_a_runtime_owned_by_another_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    (root / "ownership.json").write_text(
        json.dumps(
            {
                "runtime_root": str(root),
                "owner_task": "#22948",
                "owner_session": "someone-else",
            }
        )
    )
    monkeypatch.setattr(c0, "DEFAULT_RUNTIME_ROOT", root)

    with pytest.raises(EvidenceError, match="owner mismatch"):
        c0.main(["rerun2611a", "--owner-session", "7674aa11-70a3-40d5-91dd-1fe46140732b"])

    assert not (root / "c0").exists()
    assert not (root / "receipts").exists()
