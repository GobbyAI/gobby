"""Tests for the gclient crates.io unpublished-dependency preflight."""

from __future__ import annotations

import importlib.util
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.error import HTTPError

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "scripts"
    / "require_gobby_terminal_on_crates_io.py"
)


def _load_preflight() -> ModuleType:
    spec = importlib.util.spec_from_file_location("require_gobby_terminal_on_crates_io", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(version: str) -> str:
    return (
        "[package]\n"
        'name = "gobby-client"\n'
        'version = "0.1.0"\n'
        "\n"
        "[dependencies]\n"
        f'gobby-terminal = {{ path = "../gterminal", version = "{version}", '
        "default-features = false }\n"
    )


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_gobby_terminal_dependency_version_reads_gclient_manifest() -> None:
    preflight = _load_preflight()
    repo_root = Path(__file__).resolve().parents[2]
    version = preflight.gobby_terminal_dependency_version(
        (repo_root / "crates" / "gclient" / "Cargo.toml").read_text(encoding="utf-8")
    )
    assert version == "0.1.0"


def test_unpublished_gobby_terminal_dependency_fails_before_package(
    tmp_path: Path,
) -> None:
    preflight = _load_preflight()
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text(_manifest("0.1.0"), encoding="utf-8")

    def fake_open(_request: object, timeout: int = 0) -> Any:
        raise HTTPError(
            "https://crates.io/api/v1/crates/gobby-terminal/0.1.0",
            404,
            "Not Found",
            EmailMessage(),
            BytesIO(),
        )

    with pytest.raises(preflight.DependencyUnpublishedError, match="unpublished"):
        preflight.require_gobby_terminal_on_crates_io(manifest, opener=fake_open)


def test_yanked_gobby_terminal_dependency_fails(tmp_path: Path) -> None:
    preflight = _load_preflight()
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text(_manifest("0.1.0"), encoding="utf-8")

    def fake_open(_request: object, timeout: int = 0) -> _FakeResponse:
        return _FakeResponse(b'{"version":{"num":"0.1.0","yanked":true}}')

    with pytest.raises(preflight.DependencyUnpublishedError, match="yanked"):
        preflight.require_gobby_terminal_on_crates_io(manifest, opener=fake_open)


def test_published_gobby_terminal_dependency_succeeds(tmp_path: Path) -> None:
    preflight = _load_preflight()
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text(_manifest("0.1.0"), encoding="utf-8")

    def fake_open(_request: object, timeout: int = 0) -> _FakeResponse:
        return _FakeResponse(b'{"version":{"num":"0.1.0","yanked":false}}')

    assert preflight.require_gobby_terminal_on_crates_io(manifest, opener=fake_open) == "0.1.0"
