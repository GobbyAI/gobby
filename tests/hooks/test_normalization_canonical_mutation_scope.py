from __future__ import annotations

import pytest

from gobby.hooks._normalization_canonical import (
    _classify_shell_segment_without_redirection,
    _merge_shell_segment_metadata,
    _ShellSegmentMetadata,
)

pytestmark = pytest.mark.unit


def test_mixed_unexpanded_mutation_paths_mark_scope_unknown() -> None:
    metadata = _merge_shell_segment_metadata(
        [
            _ShellSegmentMetadata(
                kind="write",
                paths=("src/ok.py", "$UNEXPANDED/file.py"),
                repo_mutation=True,
            )
        ]
    )

    assert metadata["_canonical_repo_mutation_scope_unknown"] is True
    assert metadata["canonical_file_paths"] == ["src/ok.py"]
    assert metadata["canonical_repo_mutation"] is True


def test_git_add_keeps_paths_after_flags_and_skips_chmod_values() -> None:
    all_flag = _classify_shell_segment_without_redirection(
        ["git", "add", "-A", "src/ok.py"],
        cwd=None,
    )
    force_flag = _classify_shell_segment_without_redirection(
        ["git", "add", "-f", "src/ok.py"],
        cwd=None,
    )
    chmod = _classify_shell_segment_without_redirection(
        ["git", "add", "--chmod", "+x", "src/ok.py"],
        cwd=None,
    )

    assert all_flag.paths == ("src/ok.py",)
    assert force_flag.paths == ("src/ok.py",)
    assert chmod.paths == ("src/ok.py",)
