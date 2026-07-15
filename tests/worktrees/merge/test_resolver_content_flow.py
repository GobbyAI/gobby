"""Tests covering the resolved-content propagation path end-to-end.

These tests enforce that AI-resolved merge content actually flows from the
LLM through `MergeResolver.resolve_file` and into the `resolved_content_by_file`
field of `MergeResult` — replacing the prior placeholder behaviour where the
content was discarded.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.features import MergeResolutionConfig
from gobby.worktrees.merge.resolver import (
    MergeResolver,
    MergeResult,
    ResolutionTier,
    clean_ai_source_response,
    splice_resolutions_into_file,
)

pytestmark = pytest.mark.unit


# --- splice_resolutions_into_file ---


def test_splice_single_block() -> None:
    file_with_markers = "header\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature\nfooter\n"
    spliced = splice_resolutions_into_file(file_with_markers, ["RESOLVED"])
    assert spliced == "header\nRESOLVED\nfooter\n"


def test_splice_multiple_blocks_in_order() -> None:
    file_with_markers = (
        "<<<<<<< HEAD\n"
        "a1\n"
        "=======\n"
        "a2\n"
        ">>>>>>> b\n"
        "middle\n"
        "<<<<<<< HEAD\n"
        "c1\n"
        "=======\n"
        "c2\n"
        ">>>>>>> b\n"
        "tail\n"
    )
    spliced = splice_resolutions_into_file(file_with_markers, ["A_OK", "C_OK"])
    assert spliced == "A_OK\nmiddle\nC_OK\ntail\n"


def test_splice_count_mismatch_returns_none() -> None:
    file_with_markers = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> b\n"
    # Two resolutions provided, but file only has one block.
    assert splice_resolutions_into_file(file_with_markers, ["X", "Y"]) is None


def test_splice_empty_resolution_collapses_block() -> None:
    file_with_markers = "before\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> b\nafter\n"
    spliced = splice_resolutions_into_file(file_with_markers, [""])
    assert spliced == "before\nafter\n"


def test_splice_accepts_conflict_at_eof_without_trailing_newline() -> None:
    file_with_markers = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature"

    assert splice_resolutions_into_file(file_with_markers, ["resolved"]) == "resolved\n"


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        pytest.param("Heading\n=======\n", "Heading\n=======", id="bare-separator"),
        pytest.param("resolved\n||||||| base\nold\n", None, id="diff3-marker"),
        pytest.param("", "", id="empty-file"),
    ],
)
def test_clean_ai_source_response_handles_bare_separator_diff3_and_empty_content(
    response: str, expected: str | None
) -> None:
    assert clean_ai_source_response(response) == expected


# --- MergeResult contract ---


def test_merge_result_default_resolved_content_by_file_is_empty() -> None:
    result = MergeResult(success=False, tier=ResolutionTier.HUMAN_REVIEW, conflicts=[])
    assert result.resolved_content_by_file == {}


def test_merge_result_to_dict_includes_resolved_content_by_file() -> None:
    result = MergeResult(
        success=True,
        tier=ResolutionTier.FULL_FILE_AI,
        conflicts=[],
        resolved_content_by_file={"a.py": "resolved\n"},
    )
    payload = result.to_dict()
    assert payload["resolved_content_by_file"] == {"a.py": "resolved\n"}


# --- MergeResolver.resolve_file populates resolved_content_by_file ---


@pytest.fixture
def resolver_with_llm() -> MergeResolver:
    return MergeResolver(
        llm_service=MagicMock(),
        config=MergeResolutionConfig(candidates=["claude/sonnet"]),
    )


@pytest.mark.asyncio
async def test_resolve_file_tier2_populates_content(
    resolver_with_llm: MergeResolver, tmp_path: Path
) -> None:
    file_path = tmp_path / "small.py"
    file_path.write_text(
        "x = 1\n<<<<<<< HEAD\ny_ours = 2\n=======\ny_theirs = 3\n>>>>>>> feature\nz = 4\n"
    )

    assert resolver_with_llm.llm_service is not None
    resolver_with_llm.llm_service.call_feature = AsyncMock(return_value="y = 2 + 3")

    hunks = [{"ours": "y_ours = 2", "theirs": "y_theirs = 3"}]
    result = await resolver_with_llm.resolve_file(file_path, hunks)

    assert result.success is True
    assert result.tier == ResolutionTier.CONFLICT_ONLY_AI
    assert str(file_path) in result.resolved_content_by_file
    spliced = result.resolved_content_by_file[str(file_path)]
    assert spliced == "x = 1\ny = 2 + 3\nz = 4\n"
    assert "<<<<<<<" not in spliced


@pytest.mark.asyncio
async def test_resolve_file_tier2_preserves_intentional_empty_hunk(
    resolver_with_llm: MergeResolver, tmp_path: Path
) -> None:
    file_path = tmp_path / "small.py"
    file_path.write_text(
        "before\n"
        "<<<<<<< HEAD\nremove_me()\n=======\nremove_me()\n>>>>>>> feature\n"
        "middle\n"
        "<<<<<<< HEAD\nold()\n=======\nnew()\n>>>>>>> feature\n"
        "after\n"
    )

    assert resolver_with_llm.llm_service is not None
    resolver_with_llm.llm_service.call_feature = AsyncMock(
        return_value="__GOBBY_EMPTY_HUNK__\n---HUNK SEPARATOR---\nnew()"
    )

    hunks = [
        {"ours": "remove_me()", "theirs": "remove_me()"},
        {"ours": "old()", "theirs": "new()"},
    ]
    result = await resolver_with_llm.resolve_file(file_path, hunks)

    assert result.success is True
    assert result.resolved_content_by_file[str(file_path)] == "before\nmiddle\nnew()\nafter\n"


@pytest.mark.asyncio
async def test_resolve_file_uses_worktree_path_for_relative_file(
    resolver_with_llm: MergeResolver, tmp_path: Path
) -> None:
    file_path = tmp_path / "src" / "small.py"
    file_path.parent.mkdir()
    file_path.write_text(
        "x = 1\n<<<<<<< HEAD\ny_ours = 2\n=======\ny_theirs = 3\n>>>>>>> feature\nz = 4\n"
    )

    assert resolver_with_llm.llm_service is not None
    resolver_with_llm.llm_service.call_feature = AsyncMock(return_value="y = 2 + 3")

    hunks = [{"ours": "y_ours = 2", "theirs": "y_theirs = 3"}]
    result = await resolver_with_llm.resolve_file("src/small.py", hunks, worktree_path=tmp_path)

    assert result.success is True
    assert result.resolved_content_by_file["src/small.py"] == "x = 1\ny = 2 + 3\nz = 4\n"


@pytest.mark.asyncio
async def test_resolve_file_tier3_populates_content_when_tier2_fails(
    resolver_with_llm: MergeResolver, tmp_path: Path
) -> None:
    file_path = tmp_path / "big.py"
    # File holds one conflict block; Tier 2 LLM returns mismatched hunk count
    # (two separator-delimited chunks), forcing Tier 3.
    file_path.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature\n")

    full_file_response = "RESOLVED FULL FILE"
    tier2_response = "chunk1\n---HUNK SEPARATOR---\nchunk2\n"
    assert resolver_with_llm.llm_service is not None
    resolver_with_llm.llm_service.call_feature = AsyncMock(
        side_effect=[tier2_response, full_file_response]
    )

    hunks = [{"ours": "ours", "theirs": "theirs"}]
    result = await resolver_with_llm.resolve_file(file_path, hunks)

    assert result.success is True
    assert result.tier == ResolutionTier.FULL_FILE_AI
    assert result.resolved_content_by_file[str(file_path)] == full_file_response


@pytest.mark.asyncio
async def test_resolve_file_tier3_preserves_empty_file(
    resolver_with_llm: MergeResolver, tmp_path: Path
) -> None:
    file_path = tmp_path / "deleted.py"
    file_path.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature\n")

    assert resolver_with_llm.llm_service is not None
    resolver_with_llm.llm_service.call_feature = AsyncMock(
        side_effect=["first\n---HUNK SEPARATOR---\nsecond", ""]
    )

    result = await resolver_with_llm.resolve_file(
        file_path,
        [{"ours": "ours", "theirs": "theirs"}],
    )

    assert result.success is True
    assert result.tier == ResolutionTier.FULL_FILE_AI
    assert result.resolved_content_by_file == {str(file_path): ""}


@pytest.mark.asyncio
async def test_resolve_file_human_review_has_empty_content(
    resolver_with_llm: MergeResolver, tmp_path: Path
) -> None:
    file_path = tmp_path / "stuck.py"
    file_path.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature\n")
    assert resolver_with_llm.llm_service is not None
    resolver_with_llm.llm_service.call_feature = AsyncMock(return_value=None)

    hunks = [{"ours": "ours", "theirs": "theirs"}]
    result = await resolver_with_llm.resolve_file(file_path, hunks)

    assert result.success is False
    assert result.tier == ResolutionTier.HUMAN_REVIEW
    assert result.resolved_content_by_file == {}
