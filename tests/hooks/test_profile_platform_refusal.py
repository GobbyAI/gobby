from __future__ import annotations

import sys

import pytest

from gobby.hooks.event_handlers._session_start import profile
from gobby.paths import FilesHomeUnsupportedPlatformError


def _fail_remote_profile() -> str:
    raise AssertionError("remote profile fetch must not run on a platform refusal")


def _fail_publish(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("profile publish must not run on a platform refusal")


def test_read_user_profile_content_returns_empty_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(profile, "_read_remote_profile", _fail_remote_profile)
    assert profile.read_user_profile_content() == ""


def test_write_user_profile_content_propagates_windows_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(profile, "publish_files_home_descendant", _fail_publish)
    with pytest.raises(FilesHomeUnsupportedPlatformError):
        profile.write_user_profile_content("hello")
