"""Use the same mode and origin contract vectors as the Rust bootstrap parser."""

import json
from pathlib import Path
from typing import Any, TypedDict, cast

import pytest

from gobby.config.bootstrap import BootstrapConfigError, bootstrap_from_mapping


class ModeCase(TypedDict, total=False):
    name: str
    bootstrap: dict[str, Any]
    mode: str
    hub_daemon_url: str | None
    hub_cert: str | None
    database_url: str | None
    api_key: str | None
    api_key_id: str | None
    daemon_url: str | None
    files_home: str
    error: str


_CORPUS = Path(__file__).parents[1] / "fixtures" / "bootstrap_run_modes.json"
_CASES = cast(list[ModeCase], json.loads(_CORPUS.read_text(encoding="utf-8"))["cases"])


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_shared_vectors_match_rust(case: ModeCase) -> None:
    if "error" in case:
        with pytest.raises(BootstrapConfigError) as caught:
            bootstrap_from_mapping(case["bootstrap"])
        assert str(caught.value) == case["error"]
    else:
        config = bootstrap_from_mapping(case["bootstrap"])
        assert config.run_mode() == case["mode"]
        assert config.hub_daemon_url == case["hub_daemon_url"]
        assert config.hub_cert == case.get("hub_cert")
        assert config.database_url == case.get("database_url")
        assert config.api_key == case.get("api_key")
        assert config.api_key_id == case.get("api_key_id")
        assert config.daemon_url == case.get("daemon_url")
        if "files_home" in case:
            assert config.files_home == case["files_home"]


@pytest.mark.parametrize("value", [42, True, [], {}])
def test_non_string_files_home_names_expected_type(value: object) -> None:
    with pytest.raises(BootstrapConfigError) as caught:
        bootstrap_from_mapping({"files_home": value})
    assert str(caught.value) == "files_home must be a string"
