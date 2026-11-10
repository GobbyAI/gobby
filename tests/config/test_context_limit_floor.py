"""The additionalContext limit floor is derived from the context label bound, not a fixed number."""

import pytest
from pydantic import ValidationError

from gobby.config.hooks import HookTimeoutConfig
from gobby.llm.sdk_utils import minimum_additional_context_limit

pytestmark = pytest.mark.unit


def test_a_limit_at_the_derived_minimum_is_accepted() -> None:
    minimum = minimum_additional_context_limit()

    assert HookTimeoutConfig(additional_context_limit=minimum).additional_context_limit == minimum


def test_a_limit_below_the_derived_minimum_is_rejected_and_the_minimum_is_named() -> None:
    minimum = minimum_additional_context_limit()

    with pytest.raises(ValidationError, match=str(minimum)):
        HookTimeoutConfig(additional_context_limit=minimum - 1)


def test_a_provider_limit_below_the_derived_minimum_is_rejected() -> None:
    minimum = minimum_additional_context_limit()

    with pytest.raises(ValidationError, match=str(minimum)):
        HookTimeoutConfig(additional_context_limits={"claude": minimum - 1})
