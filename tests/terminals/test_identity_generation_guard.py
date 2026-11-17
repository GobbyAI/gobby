from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_NOT_TERMINAL_IDENTITIES = {
    ("src/gobby/terminals/leases.py", "self.daemon_epoch = str(uuid4())"),
    # The daemon-minted id of one in-place host upgrade attempt.
    ("src/gobby/terminals/host_upgrade.py", "attempt_id = uuid.uuid4().hex"),
}


def test_identity_generation_absent_from_runtimes() -> None:
    runtime_root = ROOT / "src/gobby/terminals"
    hits: list[str] = []
    for path in runtime_root.rglob("*.py"):
        relative = str(path.relative_to(ROOT))
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "uuid.uuid4" not in line and "uuid4()" not in line:
                continue
            if (relative, line.strip()) in _NOT_TERMINAL_IDENTITIES:
                continue
            hits.append(f"{relative}:{lineno}:{line.strip()}")
    assert hits == [], f"TerminalRuntime modules mint identities: {hits}"
