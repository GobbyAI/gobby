"""OCR is an exact-version required dependency, reported like SRT when included."""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

import pytest

from gobby.utils import dependency_requirements as requirements
from gobby.utils import spawn
from gobby.utils.dependency_requirements import OCR_VERSION, ocr_dependency_status

pytestmark = pytest.mark.unit


@pytest.fixture
def ocr_on_path(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"path": "/synthetic/bin/ocr", "output": None, "returncode": 0}

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        output = state["output"]
        if output is None:
            output = f"open-code-review v{OCR_VERSION} (abb7abc) darwin/arm64\n"
        return subprocess.CompletedProcess(argv, state["returncode"], stdout=output, stderr="")

    monkeypatch.setattr(shutil, "which", lambda _name: state["path"])
    monkeypatch.setattr(spawn, "run", run)
    return state


def test_pinned_ocr_is_healthy(ocr_on_path: dict[str, Any]) -> None:
    status = ocr_dependency_status()

    assert status.state == "healthy"
    assert status.installed_version == OCR_VERSION
    assert status.expected_version == OCR_VERSION
    assert status.path == "/synthetic/bin/ocr"
    assert status.error is None


def test_missing_ocr_names_the_install_action(ocr_on_path: dict[str, Any]) -> None:
    ocr_on_path["path"] = None

    status = ocr_dependency_status()

    assert status.state == "missing"
    assert status.error is not None
    assert f"requires =={OCR_VERSION}" in status.error
    assert "gobby install" in status.error


def test_other_ocr_version_is_outdated(ocr_on_path: dict[str, Any]) -> None:
    ocr_on_path["output"] = "open-code-review v1.12.0 (0000000) darwin/arm64\n"

    status = ocr_dependency_status()

    assert status.state == "outdated"
    assert status.installed_version == "1.12.0"
    assert status.error is not None
    assert "1.12.0" in status.error


def test_unreadable_version_output_is_invalid(ocr_on_path: dict[str, Any]) -> None:
    ocr_on_path["output"] = "garbled"

    status = ocr_dependency_status()

    assert status.state == "invalid"
    assert status.error is not None


def test_failed_version_probe_is_invalid(ocr_on_path: dict[str, Any]) -> None:
    ocr_on_path["returncode"] = 2
    ocr_on_path["output"] = ""

    status = ocr_dependency_status()

    assert status.state == "invalid"


def test_report_includes_ocr_only_when_requested(ocr_on_path: dict[str, Any]) -> None:
    included = requirements.collect_dependency_report(
        managed_services=False, include_srt=False, include_ocr=True
    )
    excluded = requirements.collect_dependency_report(managed_services=False, include_srt=False)

    assert included.required["ocr"].state == "healthy"
    assert "ocr" not in excluded.required
