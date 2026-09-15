"""Shell lexing helpers shared by validation detection and rule conditions."""

from __future__ import annotations

import shlex

import pytest

from gobby.config import shell_lexing
from gobby.config.shell_lexing import safe_split

pytestmark = pytest.mark.unit


def test_safe_split_caches_tokens_and_returns_fresh_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real_split = shlex.split

    def counting_split(value: str) -> list[str]:
        calls.append(value)
        return list(real_split(value))

    monkeypatch.setattr(shlex, "split", counting_split)
    shell_lexing.clear_split_cache()
    command = "uv run pytest tests/one.py -q"

    first = safe_split(command)
    second = safe_split(command)

    assert first == ["uv", "run", "pytest", "tests/one.py", "-q"]
    assert second == first
    assert second is not first
    assert calls == [command]
    first.append("mutated")
    assert safe_split(command) == ["uv", "run", "pytest", "tests/one.py", "-q"]
    assert calls == [command]
    assert safe_split("echo 'unbalanced") == ["echo", "'unbalanced"]
