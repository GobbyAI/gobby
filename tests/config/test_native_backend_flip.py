"""Gate the native terminal default on ordered host-driven CI evidence.

Linux is deferred by user decision (2026-09-16): the required OS set is macOS
only. Linux rows stay recorded in the evidence document, but they neither
qualify the flip on their own nor invalidate macOS evidence. Reinstating Linux
means putting `linux` back into `_REQUIRED_OSES`; the checker then demands a
same-commit green row for every required OS again.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gobby.config.terminals import TerminalConfig

pytestmark = pytest.mark.unit

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_EVIDENCE_PATH = _REPOSITORY_ROOT / "docs" / "evidence" / "native-backend-flip.md"
_GUIDE_PATH = _REPOSITORY_ROOT / "docs" / "guides" / "gterminal-development-guide.md"
_REQUIRED_OSES = frozenset({"macos"})
_EVIDENCE_HEADER = """\
# Native backend default-flip evidence

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
        normalized_os = operating_system.casefold()
        if normalized_os not in _REQUIRED_OSES:
            continue
        normalized_result = result.casefold()
        if normalized_result == "red":
            last_red_position = position
            continue
        if normalized_result == "green":
            green_positions_by_commit.setdefault(commit.strip("`"), {})[normalized_os] = position

    return any(
        _REQUIRED_OSES.issubset(green_positions)
        and min(green_positions.values()) > last_red_position
        for green_positions in green_positions_by_commit.values()
    )


def _evidence(*rows: str) -> str:
    return "\n".join((_EVIDENCE_HEADER, *rows))


_MACOS_GREEN = "| 2026-09-12 | macOS | `abc1234` | https://ci.example/1 | green |"
_MACOS_GREEN_LATER = "| 2026-09-14 | macOS | `def5678` | https://ci.example/4 | green |"
_MACOS_RED = "| 2026-09-13 | macOS | `def5678` | https://ci.example/2 | red |"
_LINUX_GREEN = "| 2026-09-12 | Linux | `abc1234` | https://ci.example/3 | green |"
_LINUX_GREEN_OTHER_COMMIT = "| 2026-09-12 | Linux | `def5678` | https://ci.example/3 | green |"
_LINUX_RED = "| 2026-09-13 | Linux | `def5678` | https://ci.example/5 | red |"


@pytest.mark.parametrize(
    ("default_backend", "rows", "expected"),
    [
        pytest.param("tmux", (), True, id="tmux-allows-empty-stub"),
        pytest.param("native", (), False, id="native-rejects-empty-stub"),
        pytest.param("ssh", (_MACOS_GREEN,), False, id="unknown-backend-rejected"),
        pytest.param("native", (_MACOS_GREEN,), True, id="native-allows-macos-only-green"),
        pytest.param("native", (_LINUX_GREEN,), False, id="native-rejects-linux-only-green"),
        pytest.param(
            "native",
            (_MACOS_GREEN, _LINUX_GREEN),
            True,
            id="native-allows-same-commit-green-pair",
        ),
        pytest.param(
            "native",
            (_MACOS_GREEN, _LINUX_GREEN_OTHER_COMMIT),
            True,
            id="native-ignores-linux-commit-mismatch",
        ),
        pytest.param(
            "native",
            (_MACOS_GREEN, _LINUX_RED),
            True,
            id="native-ignores-later-red-linux-row",
        ),
        pytest.param(
            "native",
            (_MACOS_GREEN, _MACOS_RED),
            False,
            id="native-rejects-later-red-macos-row",
        ),
        pytest.param(
            "native",
            (_MACOS_GREEN, _MACOS_RED, _MACOS_GREEN_LATER),
            True,
            id="native-allows-macos-green-after-red",
        ),
    ],
)
def test_flip_gate_rules(
    default_backend: str,
    rows: tuple[str, ...],
    expected: bool,
) -> None:
    assert check_native_backend_flip(_evidence(*rows), default_backend) is expected


def test_linux_deferral_relaxation() -> None:
    """macOS alone carries the gate while Linux is deferred."""
    assert _REQUIRED_OSES == frozenset({"macos"})
    assert check_native_backend_flip(_evidence(_MACOS_GREEN), "native")
    assert check_native_backend_flip(_evidence(_MACOS_GREEN, _LINUX_RED), "native")
    assert not check_native_backend_flip(_evidence(_LINUX_GREEN), "native")
    assert "Linux deferral" in _EVIDENCE_PATH.read_text(encoding="utf-8")


def test_reinstating_linux_restores_the_same_commit_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every required OS must go green at one commit after the last red row."""
    monkeypatch.setattr(sys.modules[__name__], "_REQUIRED_OSES", frozenset({"macos", "linux"}))

    assert not check_native_backend_flip(_evidence(_MACOS_GREEN), "native")
    assert check_native_backend_flip(_evidence(_MACOS_GREEN, _LINUX_GREEN), "native")
    assert not check_native_backend_flip(
        _evidence(_MACOS_GREEN, _LINUX_RED, _LINUX_GREEN), "native"
    )


def test_checked_in_flip_gate_artifacts() -> None:
    default_backend = TerminalConfig().default_backend
    evidence_text = _EVIDENCE_PATH.read_text(encoding="utf-8")
    guide_text = _GUIDE_PATH.read_text(encoding="utf-8")
    backend_status = guide_text.split("## Backend status", maxsplit=1)[1].split(
        "\n## ", maxsplit=1
    )[0]

    assert default_backend == "native"
    assert "qualifying run recorded" in evidence_text
    assert check_native_backend_flip(evidence_text, default_backend)
    assert "native-backend-flip.md" in backend_status
    assert "`native` is the default backend" in backend_status
    assert "`tmux` remains supported" in backend_status
    assert "gobby config set terminals.default_backend tmux" in backend_status
