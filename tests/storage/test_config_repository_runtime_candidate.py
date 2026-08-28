from unittest.mock import MagicMock

import pytest

from gobby.config.feature_base import candidate_labels
from gobby.storage.config_repository import ConfigRepository

LOW_CANDIDATES = ("codex/gpt-5.6-luna", "claude/haiku")


@pytest.mark.unit
def test_runtime_candidate_inherits_sparse_profile_default() -> None:
    config = ConfigRepository(MagicMock()).runtime_candidate(
        {"ai.generation.profile_defaults.feature_low": list(LOW_CANDIDATES)},
        {},
    )

    assert candidate_labels(config.session_summary.candidates) == LOW_CANDIDATES


@pytest.mark.unit
def test_runtime_candidate_preserves_explicit_feature_candidates() -> None:
    config = ConfigRepository(MagicMock()).runtime_candidate(
        {
            "ai.generation.profile_defaults.feature_low": list(LOW_CANDIDATES),
            "session_summary.candidates": ["claude/haiku"],
        },
        {},
    )

    assert candidate_labels(config.session_summary.candidates) == ("claude/haiku",)
