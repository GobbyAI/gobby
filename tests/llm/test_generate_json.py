"""Tests for generate_json() on LLM providers."""

import pytest

pytestmark = pytest.mark.unit


class TestLLMProviderBaseGenerateJson:
    """Tests for LLMProvider abstract generate_json method."""

    def test_generate_json_is_abstract(self) -> None:
        """generate_json is declared on LLMProvider base class."""
        from gobby.llm.base import LLMProvider

        assert hasattr(LLMProvider, "generate_json")

    def test_generate_json_signature(self) -> None:
        """generate_json accepts prompt, system_prompt, model, and caller params."""
        import inspect

        from gobby.llm.base import LLMProvider

        sig = inspect.signature(LLMProvider.generate_json)
        params = list(sig.parameters.keys())
        assert "prompt" in params
        assert "system_prompt" in params
        assert "model" in params
        assert "caller" in params
