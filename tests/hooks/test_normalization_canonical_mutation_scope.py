from __future__ import annotations

import pytest

from gobby.hooks._normalization_canonical import (
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
