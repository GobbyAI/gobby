from __future__ import annotations

import pytest

from gobby.hooks._normalization_operands import _curl_output_paths

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (["curl", "-O", "https://example.test/archive.tgz"], (True, [])),
        (
            [
                "curl",
                "--remote-name",
                "--output-dir",
                "downloads",
                "https://example.test/archive.tgz",
            ],
            (True, ["downloads"]),
        ),
        (["curl", "-K", "curl.conf"], (True, [])),
        (["curl", "--config=curl.conf"], (True, [])),
        (
            ["curl", "-sLoartifact.tgz", "https://example.test/archive.tgz"],
            (True, ["artifact.tgz"]),
        ),
        (["curl", "-sO", "https://example.test/archive.tgz"], (True, [])),
        (["curl", "-sKcurl.conf"], (True, [])),
    ],
)
def test_curl_output_classifier_branches(
    parts: list[str],
    expected: tuple[bool, list[str]],
) -> None:
    assert _curl_output_paths(parts) == expected
