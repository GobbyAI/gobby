from pathlib import Path

import pytest

from gobby.ask.permissions import UnsupportedAskRuntime, compile_ask_runtime_profile


def test_unvalidated_native_profile_is_refused(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    scratch_root = tmp_path / "scratch"
    source_root.mkdir()
    scratch_root.mkdir()

    with pytest.raises(UnsupportedAskRuntime, match="validation"):
        compile_ask_runtime_profile(
            provider="claude",
            source_root=source_root,
            scratch_root=scratch_root,
        )
