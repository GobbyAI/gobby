from __future__ import annotations

from typing import get_args

import pytest

from gobby.llm.base import AuthMode, LLMProviderCancellation, LLMTextResult

pytestmark = pytest.mark.unit


def test_text_result_preserves_usage_and_selection_metadata() -> None:
    result = LLMTextResult(
        text="done",
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        provider="claude",
        model="haiku",
        profile="feature_low",
    )

    assert result.text == "done"
    assert result.usage == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    assert result.provider == "claude"
    assert result.model == "haiku"
    assert result.profile == "feature_low"


def test_provider_cancellation_is_runtime_error() -> None:
    error = LLMProviderCancellation("cancelled")

    assert isinstance(error, RuntimeError)
    assert str(error) == "cancelled"


def test_auth_mode_values_remain_documented() -> None:
    assert get_args(AuthMode) == ("subscription", "api_key", "adc")
