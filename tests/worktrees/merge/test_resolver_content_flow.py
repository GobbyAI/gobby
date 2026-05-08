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

from gobby.worktrees.merge.resolver import (
    MergeResolver,
    MergeResult,
    ResolutionTier,
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
    return MergeResolver(llm_service=MagicMock())


@pytest.mark.asyncio
async def test_resolve_file_tier2_populates_content(
    resolver_with_llm: MergeResolver, tmp_path: Path
) -> None:
    file_path = tmp_path / "small.py"
    file_path.write_text(
        "x = 1\n<<<<<<< HEAD\ny_ours = 2\n=======\ny_theirs = 3\n>>>>>>> feature\nz = 4\n"
    )

    provider = MagicMock()
    provider.generate_text = AsyncMock(return_value="y = 2 + 3")
    assert resolver_with_llm.llm_service is not None
    resolver_with_llm.llm_service.get_default_provider.return_value = provider

    hunks = [{"ours": "y_ours = 2", "theirs": "y_theirs = 3"}]
    result = await resolver_with_llm.resolve_file(file_path, hunks)

    assert result.success is True
    assert result.tier == ResolutionTier.CONFLICT_ONLY_AI
    assert str(file_path) in result.resolved_content_by_file
    spliced = result.resolved_content_by_file[str(file_path)]
    assert spliced == "x = 1\ny = 2 + 3\nz = 4\n"
    assert "<<<<<<<" not in spliced


@pytest.mark.asyncio
async def test_resolve_file_uses_worktree_path_for_relative_file(
    resolver_with_llm: MergeResolver, tmp_path: Path
) -> None:
    file_path = tmp_path / "src" / "small.py"
    file_path.parent.mkdir()
    file_path.write_text(
        "x = 1\n<<<<<<< HEAD\ny_ours = 2\n=======\ny_theirs = 3\n>>>>>>> feature\nz = 4\n"
    )

    provider = MagicMock()
    provider.generate_text = AsyncMock(return_value="y = 2 + 3")
    assert resolver_with_llm.llm_service is not None
    resolver_with_llm.llm_service.get_default_provider.return_value = provider

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

    full_file_response = "RESOLVED FULL FILE\n"
    tier2_response = "chunk1\n---HUNK SEPARATOR---\nchunk2\n"
    provider = MagicMock()
    provider.generate_text = AsyncMock(side_effect=[tier2_response, full_file_response])
    assert resolver_with_llm.llm_service is not None
    resolver_with_llm.llm_service.get_default_provider.return_value = provider

    hunks = [{"ours": "ours", "theirs": "theirs"}]
    result = await resolver_with_llm.resolve_file(file_path, hunks)

    assert result.success is True
    assert result.tier == ResolutionTier.FULL_FILE_AI
    assert result.resolved_content_by_file[str(file_path)] == full_file_response


@pytest.mark.asyncio
async def test_resolve_file_human_review_has_empty_content(
    resolver_with_llm: MergeResolver, tmp_path: Path
) -> None:
    file_path = tmp_path / "stuck.py"
    file_path.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature\n")
    provider = MagicMock()
    provider.generate_text = AsyncMock(return_value=None)
    assert resolver_with_llm.llm_service is not None
    resolver_with_llm.llm_service.get_default_provider.return_value = provider

    hunks = [{"ours": "ours", "theirs": "theirs"}]
    result = await resolver_with_llm.resolve_file(file_path, hunks)

    assert result.success is False
    assert result.tier == ResolutionTier.HUMAN_REVIEW
    assert result.resolved_content_by_file == {}
