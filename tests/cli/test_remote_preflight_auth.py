"""Remote installer authentication uses its explicit bootstrap API key."""

from pathlib import Path
from typing import cast

import httpx
import pytest

from gobby.cli.installers import remote_preflight


def test_remote_probe_reads_bootstrap_key_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "remote-home"
    home.mkdir()
    bootstrap = home / "bootstrap.yaml"
    bootstrap.write_text("api_key: operator-one\n")
    (home / ".secret_kek").write_text("test-kek")
    monkeypatch.setenv("GOBBY_AGENT_API_TOKEN", "managed-token-must-not-be-used")
    observed: list[dict[str, str]] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        assert url == "http://hub.example.test:60887/api/files/user-md"
        observed.append(cast(dict[str, str], kwargs["headers"]).copy())
        return httpx.Response(200, json={"content": "owner profile"})

    monkeypatch.setattr("gobby.cli.installers.remote_preflight.httpx.get", get)
    assert remote_preflight._credential_errors(home) == []
    assert (
        remote_preflight.probe_hub_user_md("http://hub.example.test:60887", gobby_home=home) == []
    )
    bootstrap.write_text("api_key: operator-two\n")
    assert (
        remote_preflight.probe_hub_user_md("http://hub.example.test:60887", gobby_home=home) == []
    )
    assert observed == [
        {"Authorization": "Bearer operator-one", "X-Gobby-Files-Proxy-Hop": "1"},
        {"Authorization": "Bearer operator-two", "X-Gobby-Files-Proxy-Hop": "1"},
    ]


@pytest.mark.parametrize("contents", ["api_key: ''\n", "api_key: 42\n", "[invalid", None])
def test_remote_probe_refuses_unusable_bootstrap_key(
    contents: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "remote-home"
    home.mkdir()
    if contents is not None:
        (home / "bootstrap.yaml").write_text(contents)
    (home / ".secret_kek").write_text("test-kek")
    monkeypatch.setenv("GOBBY_AGENT_API_TOKEN", "managed-token-must-not-be-used")

    def get(*_args: object, **_kwargs: object) -> httpx.Response:
        pytest.fail("Unusable bootstrap credentials must fail before a network probe")

    monkeypatch.setattr("gobby.cli.installers.remote_preflight.httpx.get", get)
    for errors in (
        remote_preflight._credential_errors(home),
        remote_preflight.probe_hub_user_md("http://hub.example.test:60887", gobby_home=home),
    ):
        assert len(errors) == 1
        assert "bootstrap.yaml" in errors[0]
        assert "api_key" in errors[0]
