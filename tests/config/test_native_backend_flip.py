"""Gate the native terminal default on same-commit cross-platform CI evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.config.terminals import TerminalConfig

pytestmark = pytest.mark.unit

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_EVIDENCE_PATH = _REPOSITORY_ROOT / "docs" / "evidence" / "native-backend-flip.md"
_GUIDE_PATH = _REPOSITORY_ROOT / "docs" / "guides" / "gterminal-development-guide.md"
_REQUIRED_OSES = frozenset({"macos", "linux"})
_EVIDENCE_HEADER = """\
# Native backend default-flip evidence

Status: **no qualifying run**.

| Date | OS | Commit | Workflow run | Result |
| --- | --- | --- | --- | --- |"""


def check_native_backend_flip(evidence_text: str, default_backend: str) -> bool:
    """Return whether the requested default satisfies the documented flip gate."""
    if default_backend == "tmux":
        return True
    if default_backend != "native":
        return False

    green_positions_by_commit: dict[str, dict[str, int]] = {}
    last_red_position = -1
    for position, line in enumerate(evidence_text.splitlines()):
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 5:
            continue
        _, operating_system, commit, _, result = cells
        normalized_result = result.casefold()
        if normalized_result == "red":
            last_red_position = position
            continue
        normalized_os = operating_system.casefold()
        if normalized_result == "green" and normalized_os in _REQUIRED_OSES:
            green_positions_by_commit.setdefault(commit.strip("`"), {})[normalized_os] = position

    return any(
        _REQUIRED_OSES.issubset(green_positions)
        and min(green_positions.values()) > last_red_position
        for green_positions in green_positions_by_commit.values()
    )


def _evidence(*rows: str) -> str:
    return "\n".join((_EVIDENCE_HEADER, *rows))


@pytest.mark.parametrize(
    ("default_backend", "rows", "expected"),
    [
        pytest.param("tmux", (), True, id="tmux-allows-empty-stub"),
        pytest.param("native", (), False, id="native-rejects-empty-stub"),
        pytest.param(
            "native",
            ("| 2026-09-12 | macOS | `abc1234` | https://ci.example/1 | green |",),
            False,
            id="native-rejects-one-green-os",
        ),
        pytest.param(
            "native",
            (
                "| 2026-09-12 | macOS | `abc1234` | https://ci.example/1 | green |",
                "| 2026-09-12 | Linux | `def5678` | https://ci.example/2 | green |",
            ),
            False,
            id="native-rejects-different-commits",
        ),
        pytest.param(
            "native",
            (
                "| 2026-09-12 | macOS | `abc1234` | https://ci.example/1 | green |",
                "| 2026-09-12 | Linux | `abc1234` | https://ci.example/2 | green |",
            ),
            True,
            id="native-allows-same-commit-green-pair",
        ),
        pytest.param(
            "native",
            (
                "| 2026-09-12 | macOS | `abc1234` | https://ci.example/1 | green |",
                "| 2026-09-12 | Linux | `abc1234` | https://ci.example/2 | green |",
                "| 2026-09-13 | Linux | `def5678` | https://ci.example/3 | red |",
            ),
            False,
            id="native-rejects-later-red-row",
        ),
        pytest.param(
            "native",
            (
                "| 2026-09-12 | macOS | `abc1234` | https://ci.example/1 | green |",
                "| 2026-09-13 | Linux | `def5678` | https://ci.example/2 | red |",
                "| 2026-09-14 | Linux | `abc1234` | https://ci.example/3 | green |",
            ),
            False,
            id="native-rejects-red-between-green-rows",
        ),
    ],
)
def test_flip_gate_rules(
    default_backend: str,
    rows: tuple[str, ...],
    expected: bool,
) -> None:
    assert check_native_backend_flip(_evidence(*rows), default_backend) is expected


def test_checked_in_flip_gate_artifacts() -> None:
    default_backend = TerminalConfig().default_backend
    evidence_text = _EVIDENCE_PATH.read_text(encoding="utf-8")
    guide_text = _GUIDE_PATH.read_text(encoding="utf-8")
    backend_status = guide_text.split("## Backend status", maxsplit=1)[1].split(
        "\n## ", maxsplit=1
    )[0]

    assert default_backend == "tmux"
    assert "no qualifying run" in evidence_text
    assert check_native_backend_flip(evidence_text, default_backend)
    assert "native-backend-flip.md" in backend_status
    assert "macOS and Linux at the same commit" in backend_status
    assert "gobby config set terminals.default_backend tmux" in backend_status
