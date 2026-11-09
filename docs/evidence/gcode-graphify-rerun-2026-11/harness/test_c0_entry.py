"""c0.py refuses any runtime that the owned session does not hold before it runs a command."""

from __future__ import annotations

import json
from pathlib import Path

import c0
import pytest
from runtime_boundary import OWNER_TASK, EvidenceError


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
                "owner_task": OWNER_TASK,
                "owner_session": "someone-else",
            }
        )
    )
    monkeypatch.setattr(c0, "DEFAULT_RUNTIME_ROOT", root)

    with pytest.raises(EvidenceError, match="owner mismatch"):
        c0.main(["rerun2611a", "--owner-session", "7674aa11-70a3-40d5-91dd-1fe46140732b"])

    assert not (root / "c0").exists()
    assert not (root / "receipts").exists()


def test_c0_policy_reads_the_graphify_interpreter_base_and_nothing_more_writable(
    tmp_path: Path,
) -> None:
    base = tmp_path / "uv/cpython-3.13/bin/python3.13"
    base.parent.mkdir(parents=True)
    base.write_text("")
    root = tmp_path / "runtime"
    interpreter = root / "tools/graphify/venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(base)

    policy = c0.c0_policy(root, tmp_path / "home", interpreter)

    install = base.resolve().parent.parent
    assert str(install) in policy["filesystem"]["allowRead"]
    assert str(root) in policy["filesystem"]["allowRead"]
    assert policy["filesystem"]["allowWrite"] == [str(root)]
