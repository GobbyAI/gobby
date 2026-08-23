from __future__ import annotations

import pytest

from gobby.hooks._normalization_canonical import (
    _classify_shell_segment_without_redirection,
    _merge_shell_segment_metadata,
    _normalize_shell_tool_metadata,
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


def test_read_only_loop_header_paths_stay_out_of_the_mutation_set() -> None:
    """A loop header names iteration words; only a mutating segment names writes."""
    metadata = _merge_shell_segment_metadata(
        [
            _ShellSegmentMetadata(kind="execute", paths=("1", "2", "3")),
            _ShellSegmentMetadata(kind="write", paths=("/dev/null",), repo_mutation=True),
        ]
    )

    assert metadata["canonical_tool_kind"] == "write"
    assert metadata["canonical_file_paths"] == ["/dev/null"]


def test_read_only_probe_loop_is_not_an_in_project_mutation() -> None:
    """The command that mis-attributed `1`, `2`, and `3` to a claimed task."""
    metadata = _normalize_shell_tool_metadata(
        'for i in 1 2 3; do curl -s -o /dev/null -w "attempt $i" '
        "http://localhost:60887/health; done"
    )

    assert metadata["canonical_file_paths"] == ["/dev/null"]


def test_loop_header_still_scopes_a_mutating_body_with_unexpanded_paths() -> None:
    """Guard: `for f in a.py b.py; do sed -i ... "$f"; done` still attributes both.

    The header literals are the only scope signal a body with unexpanded
    operands has, which is the case the header classifier was built for.
    """
    metadata = _merge_shell_segment_metadata(
        [
            _ShellSegmentMetadata(kind="execute", paths=("a.py", "b.py")),
            _ShellSegmentMetadata(kind="write", paths=("$f",), repo_mutation=True),
        ]
    )

    assert metadata["canonical_file_paths"] == ["a.py", "b.py"]
    assert metadata["_canonical_repo_mutation_scope_unknown"] is True
