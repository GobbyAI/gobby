"""The process-listing guard admits only argument-free ps columns; ``comm`` prints arguments on macOS.

Bundled rules are enumerated generically (AGENTS.md rule 16) and matched through the engine's
own ``command_patterns_match`` with each effect's ``mask_quoted`` flag, so the test pins what a
seat's Bash call actually meets.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import gobby
from gobby.workflows.engine.command_matching import command_patterns_match

pytestmark = pytest.mark.unit

RULES_ROOT = Path(gobby.__file__).parent / "install" / "shared" / "workflows" / "rules"


def _bundled_block_patterns() -> list[tuple[str, str, bool]]:
    # A rule with a `when` predicate runs only when the predicate holds, and its pattern can be
    # deliberately broad behind it, so only unconditional Bash block effects decide a plain ps.
    found: list[tuple[str, str, bool]] = []
    for path in sorted(RULES_ROOT.rglob("*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        for rule_name, rule in (data.get("rules") or {}).items():
            if rule.get("when") is not None:
                continue
            for effect in rule.get("effects") or []:
                pattern = effect.get("command_pattern")
                if (
                    effect.get("type") == "block"
                    and "Bash" in (effect.get("tools") or [])
                    and isinstance(pattern, str)
                ):
                    mask_quoted = bool(effect.get("mask_quoted", False))
                    found.append((f"{path.name}::{rule_name}", pattern, mask_quoted))
    return found


BLOCK_PATTERNS = _bundled_block_patterns()


def _refused(command: str) -> bool:
    return any(
        command_patterns_match(command, pattern=pattern, mask_quoted=mask_quoted)
        for _, pattern, mask_quoted in BLOCK_PATTERNS
    )


@pytest.mark.parametrize(
    "command",
    [
        "ps -Ao pid,comm",
        "ps -o comm -p 1",
        "ps -axo pid,ppid,etime,comm",
        "ps -Ao pid,ppid,etime,pcpu,stat,comm",
    ],
)
def test_ps_listing_the_command_column_is_refused(command: str) -> None:
    assert BLOCK_PATTERNS, "no bundled block patterns were loaded"
    assert _refused(command)


@pytest.mark.parametrize(
    "command",
    [
        "ps -Ao pid,ppid,etime,pcpu,stat,ucomm",
        "ps -Ao pid,pcpu,pmem,ucomm -r | head -20",
        "ps -Ao pcpu,ucomm -r | head -15",
        "ps -o ucomm= -p 1",
        "ps -A -o pid=,ucomm=",
    ],
)
def test_ps_listing_the_accounting_name_is_allowed(command: str) -> None:
    assert not _refused(command)
