from __future__ import annotations

from pathlib import Path

import pytest

import gobby.deployment as deployment

pytestmark = pytest.mark.unit


def test_default_deployment_token_is_memoized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[Path] = []
    deployment._default_deployment_token.cache_clear()

    def get_home() -> Path:
        calls.append(tmp_path)
        return tmp_path

    monkeypatch.setattr(deployment, "get_gobby_home", get_home)

    first = deployment.deployment_token()
    second = deployment.deployment_token()

    assert first == second
    assert calls == [tmp_path]
    deployment._default_deployment_token.cache_clear()
