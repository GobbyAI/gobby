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
    error: str
    error_type: str


_CORPUS = Path(__file__).parents[1] / "fixtures" / "bootstrap_run_modes.json"
_CASES = cast(list[ModeCase], json.loads(_CORPUS.read_text(encoding="utf-8"))["cases"])


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_shared_vectors_match_rust(case: ModeCase) -> None:
    if "error" in case:
        error_type = ValueError if case.get("error_type") == "ValueError" else BootstrapConfigError
        with pytest.raises(error_type) as caught:
            bootstrap_from_mapping(case["bootstrap"])
        assert str(caught.value) == case["error"]
    else:
        config = bootstrap_from_mapping(case["bootstrap"])
        assert config.run_mode() == case["mode"]
        assert config.hub_daemon_url == case["hub_daemon_url"]
        assert config.hub_cert == case.get("hub_cert")
