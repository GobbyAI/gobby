"""The managed OCR CLI installs the pinned version and leaves a matching install in place."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gobby.cli.install_setup_ocr import OcrInstallError, install_ocr_cli
from gobby.utils import spawn
from gobby.utils.dependency_requirements import OCR_PACKAGE, OCR_VERSION

pytestmark = pytest.mark.unit

NPM = "/usr/local/bin/npm"


@pytest.fixture
def ocr_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Isolated PATH: `ocr` resolves to `path` and reports `version`; npm installs `installs`."""
    state: dict[str, Any] = {
        "path": None,
        "version": OCR_VERSION,
        "installs": OCR_VERSION,
        "npm_returncode": 0,
        "npm_stderr": "",
        "calls": [],
    }

    def which(name: str) -> str | None:
        if name == "npm":
            return NPM
        if name == "ocr":
            path: str | None = state["path"]
            return path
        return None

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        state["calls"].append(list(argv))
        if argv[1:] == ["--version"]:
            output = f"open-code-review v{state['version']} (abb7abc) darwin/arm64\n"
            return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")
        if argv[1:4] == ["install", "-g", f"{OCR_PACKAGE}@{OCR_VERSION}"]:
            if state["npm_returncode"] == 0:
                state["path"] = str(tmp_path / "bin" / "ocr")
                state["version"] = state["installs"]
            return subprocess.CompletedProcess(
                argv, state["npm_returncode"], stdout="", stderr=state["npm_stderr"]
            )
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(spawn, "run", run)
    return state


def _npm_install_calls(state: dict[str, Any]) -> list[list[str]]:
    return [call for call in state["calls"] if call[1:2] == ["install"]]


def test_pinned_install_is_left_in_place_on_rerun(ocr_state: dict[str, Any]) -> None:
    ocr_state["path"] = "/synthetic/bin/ocr"

    result = install_ocr_cli()

    assert result.installed is False
    assert result.version == OCR_VERSION
    assert result.path == Path("/synthetic/bin/ocr")
    assert _npm_install_calls(ocr_state) == []


def test_missing_ocr_installs_the_pinned_version(ocr_state: dict[str, Any], tmp_path: Path) -> None:
    result = install_ocr_cli()

    assert result.installed is True
    assert result.version == OCR_VERSION
    assert result.path == (tmp_path / "bin" / "ocr").resolve()
    assert _npm_install_calls(ocr_state) == [[NPM, "install", "-g", f"{OCR_PACKAGE}@{OCR_VERSION}"]]


def test_outdated_install_is_replaced_with_the_pin(ocr_state: dict[str, Any]) -> None:
    ocr_state["path"] = "/synthetic/bin/ocr"
    ocr_state["version"] = "1.12.0"

    result = install_ocr_cli()

    assert result.installed is True
    assert result.version == OCR_VERSION
    assert len(_npm_install_calls(ocr_state)) == 1


def test_npm_failure_is_an_install_error(ocr_state: dict[str, Any]) -> None:
    ocr_state["npm_returncode"] = 1
    ocr_state["npm_stderr"] = "npm ERR! ENOTFOUND registry.npmjs.org"

    with pytest.raises(OcrInstallError, match="ENOTFOUND"):
        install_ocr_cli()


def test_install_yielding_another_version_is_an_install_error(ocr_state: dict[str, Any]) -> None:
    ocr_state["installs"] = "9.9.9"

    with pytest.raises(OcrInstallError, match="9.9.9"):
        install_ocr_cli()


def test_missing_npm_is_an_install_error(ocr_state: dict[str, Any], monkeypatch: Any) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    with pytest.raises(OcrInstallError, match="npm is required"):
        install_ocr_cli()
