from gobby.config.feature_base import DEFAULT_PROFILE_CANDIDATES, FeatureProfile
from gobby.config.pipelines import PipelineConfig


def test_prompt_step_uses_low_feature_defaults() -> None:
    config = PipelineConfig()

    assert config.prompt_step.profile == FeatureProfile.LOW
    assert config.prompt_step.candidates == list(DEFAULT_PROFILE_CANDIDATES[FeatureProfile.LOW])
