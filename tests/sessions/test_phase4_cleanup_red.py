"""Red tests for phase 4 session-manager cleanup."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LEGACY_MANAGER_IMPORT = "from gobby.sessions." + "manager import SessionManager"
_DEPRECATED_ALIAS = "Local" + "SessionManager"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_hooks_source_no_longer_uses_session_storage_alias() -> None:
    """Hooks should stop carrying the legacy _session_storage name."""
    hooks_root = _repo_root() / "src" / "gobby" / "hooks"
    python_sources = hooks_root.rglob("*.py")
    legacy_refs = [
        path.relative_to(_repo_root())
        for path in python_sources
        if "_session_storage" in path.read_text(encoding="utf-8")
    ]

    assert legacy_refs == []


def test_hook_manager_keeps_exactly_one_session_manager_attribute() -> None:
    """HookManager should keep only _session_manager after cleanup."""
    source = (_repo_root() / "src" / "gobby" / "hooks" / "hook_manager.py").read_text(
        encoding="utf-8"
    )
    attrs = set(re.findall(r"self\._session_(manager|storage)\b", source))

    assert attrs == {"manager"}


def test_session_manager_service_tests_use_canonical_attribute_access() -> None:
    """Session-manager tests should stop using shim imports and dict-style assertions."""
    source = (_repo_root() / "tests" / "sessions" / "test_sessions_manager.py").read_text(
        encoding="utf-8"
    )

    assert _LEGACY_MANAGER_IMPORT not in source
    assert _DEPRECATED_ALIAS not in source
    assert ".get_session(" not in source
    assert 'session["' not in source
