"""The capability signing key is published without exposing its bytes outside denied paths."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from gobby.utils import capability_signing_key as signing

pytestmark = pytest.mark.unit


def test_ensure_stages_the_key_only_inside_the_denied_staging_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    linked_from: list[Path] = []
    real_link = os.link

    def recording_link(src: str | Path, dst: str | Path) -> None:
        linked_from.append(Path(src))
        real_link(src, dst)

    monkeypatch.setattr(os, "link", recording_link)
    key_path = tmp_path / signing.CAPABILITY_SIGNING_KEY_FILENAME

    key = signing.ensure_capability_signing_key(key_path)

    assert key is not None and len(key) == 32
    staging = signing.capability_signing_key_staging_path(key_path)
    assert [source.parent for source in linked_from] == [staging]
    assert stat.S_IMODE(os.stat(staging).st_mode) == 0o700
    assert list(staging.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([key_path.name, staging.name])


def test_ensure_refuses_a_staging_symlink(tmp_path: Path) -> None:
    key_path = tmp_path / signing.CAPABILITY_SIGNING_KEY_FILENAME
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    signing.capability_signing_key_staging_path(key_path).symlink_to(elsewhere)

    assert signing.ensure_capability_signing_key(key_path) is None
    assert not key_path.exists()
    assert list(elsewhere.iterdir()) == []
