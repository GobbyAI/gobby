import pytest

from gobby.config.feature_base import (
    FeatureProfile,
    candidate_labels,
    default_candidates_for_profile,
)
from gobby.config.pipelines import PipelineConfig

pytestmark = pytest.mark.unit


def test_prompt_step_uses_low_feature_defaults() -> None:
    config = PipelineConfig()

    assert config.prompt_step.profile == FeatureProfile.LOW
    assert candidate_labels(config.prompt_step.candidates) == default_candidates_for_profile(
        FeatureProfile.LOW
    )
