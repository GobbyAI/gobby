"""Contract for the oracle branch each communications adapter takes.

No adapter has a real account to verify against, so the five adapters that ship take the
published-API-contract branch, unverified, and Discord is deferred with its artifacts kept.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BRANCH_FILE = Path(__file__).resolve().parent / "adapter_branch.json"
ADAPTER_DIR = REPO_ROOT / "src" / "gobby" / "communications" / "adapters"
CONTRACT_BRANCH_ADAPTERS = ("telegram", "slack", "teams", "email", "sms")
DEFERRED_ADAPTERS = ("discord",)


def test_each_adapter_declares_its_oracle_branch() -> None:
    assert BRANCH_FILE.is_file(), f"the adapter branch file does not exist at {BRANCH_FILE}"
    for name in CONTRACT_BRANCH_ADAPTERS + DEFERRED_ADAPTERS:
        module = ADAPTER_DIR / f"{name}.py"
        assert module.is_file(), f"the {name} adapter module does not exist at {module}"
    document = json.loads(BRANCH_FILE.read_text(encoding="utf-8"))
    adapters = document.get("adapters")
    assert isinstance(adapters, dict), f"{BRANCH_FILE} has no adapters object"
    expected = set(CONTRACT_BRANCH_ADAPTERS + DEFERRED_ADAPTERS)
    assert set(adapters) == expected, (
        f"{BRANCH_FILE} declares {sorted(adapters)}, not {sorted(expected)}"
    )
    for name in CONTRACT_BRANCH_ADAPTERS:
        entry = adapters[name]
        assert entry == {
            "branch": "provider_api_contract",
            "account": "none",
            "verified": False,
        }, f"{name} declares {entry}"
    for name in DEFERRED_ADAPTERS:
        entry = adapters[name]
        assert entry.get("branch") == "deferred", f"{name} declares {entry}"
        assert entry.get("verified") is False, f"the deferred {name} claims verification"
