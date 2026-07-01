"""Tests for the embedding model catalog."""

from __future__ import annotations

import pytest

from gobby.ai.embedding_catalog import (
    DEFAULT_CATALOG_ID,
    PICKER_ENTRIES,
    all_keys,
    catalog_summary,
    entries_for_provider,
    get_spec,
    get_spec_or_raise,
    lmstudio_ref_for_key,
    ollama_tag_for_key,
    picker_keys,
    resolve_for_provider,
)

pytestmark = pytest.mark.unit


class TestCatalogIntegrity:
    """Verify catalog entries are consistent."""

    def test_all_picker_entries_exist(self) -> None:
        for key in PICKER_ENTRIES:
            spec = get_spec(key)
            assert spec is not None, f"Picker entry {key} not in catalog"

    def test_default_key_exists_and_is_nomic(self) -> None:
        spec = get_spec(DEFAULT_CATALOG_ID)
        assert spec is not None
        assert spec.family == "nomic"
        assert spec.dim == 768

    def test_all_keys_have_valid_specs(self) -> None:
        for key in all_keys():
            spec = get_spec(key)
            assert spec is not None
            assert spec.key == key
            assert spec.dim > 0
            assert spec.family in ("nomic", "qwen3")
            assert spec.ollama_tag
            assert spec.lmstudio_ref
            assert spec.gguf_repo
            assert spec.gguf_filename
            assert spec.gguf_sha256
            assert len(spec.gguf_sha256) == 64  # sha256 hex

    def test_quant_qualified_keys(self) -> None:
        for key in all_keys():
            # Keys should contain a quant suffix
            assert any(q in key for q in ("-q4", "-q8", "-f16")), f"Key {key} lacks quant suffix"

    def test_nomic_keys_have_none_query_prefix(self) -> None:
        for key in all_keys():
            spec = get_spec(key)
            assert spec is not None
            if spec.family == "nomic":
                assert spec.query_prefix is None

    def test_qwen3_keys_have_query_prefix(self) -> None:
        for key in all_keys():
            spec = get_spec(key)
            assert spec is not None
            if spec.family == "qwen3":
                assert spec.query_prefix is not None
                assert "Instruct:" in spec.query_prefix
                assert "Query:" in spec.query_prefix

    def test_nomic_q4_q8_not_real_on_ollama(self) -> None:
        for key in ("nomic-v1.5-q4", "nomic-v1.5-q8"):
            spec = get_spec(key)
            assert spec is not None
            assert spec.ollama_quant_real is False

    def test_nomic_f16_is_real_on_ollama(self) -> None:
        spec = get_spec("nomic-v1.5-f16")
        assert spec is not None
        assert spec.ollama_quant_real is True

    def test_qwen3_all_quants_real_on_ollama(self) -> None:
        for key in all_keys():
            spec = get_spec(key)
            assert spec is not None
            if spec.family == "qwen3":
                assert spec.ollama_quant_real is True

    def test_qwen3_lmstudio_is_experimental(self) -> None:
        for key in all_keys():
            spec = get_spec(key)
            assert spec is not None
            if spec.family == "qwen3":
                assert spec.compatibility.lmstudio == "experimental"

    def test_nomic_lmstudio_is_stable(self) -> None:
        for key in all_keys():
            spec = get_spec(key)
            assert spec is not None
            if spec.family == "nomic":
                assert spec.compatibility.lmstudio == "stable"

    def test_recommended_entries(self) -> None:
        nomic_recs = [
            k for k in all_keys() if (s := get_spec(k)) and s.recommended and s.family == "nomic"
        ]
        assert "nomic-v1.5-f16" in nomic_recs
        qwen3_recs = [
            k for k in all_keys() if (s := get_spec(k)) and s.recommended and s.family == "qwen3"
        ]
        assert "qwen3-8b-q8" in qwen3_recs

    def test_dims_match_family(self) -> None:
        expected_dims = {
            "nomic": 768,
            "qwen3-0.6b": 1024,
            "qwen3-4b": 2560,
            "qwen3-8b": 4096,
        }
        for key in all_keys():
            spec = get_spec(key)
            assert spec is not None
            for family_prefix, expected_dim in expected_dims.items():
                if key.startswith(family_prefix):
                    assert spec.dim == expected_dim, f"{key} dim {spec.dim} != {expected_dim}"


class TestCatalogLookup:
    """Test catalog lookup functions."""

    def test_get_spec_unknown_key_returns_none(self) -> None:
        assert get_spec("nonexistent-key") is None

    def test_get_spec_or_raise_unknown_key_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown embedding catalog key"):
            get_spec_or_raise("nonexistent-key")

    def test_ollama_tag_for_key(self) -> None:
        assert ollama_tag_for_key("nomic-v1.5-f16") == "nomic-embed-text"
        assert ollama_tag_for_key("qwen3-8b-q8") == "qwen3-embedding:8b-q8_0"

    def test_lmstudio_ref_for_key(self) -> None:
        assert "nomic" in lmstudio_ref_for_key("nomic-v1.5-f16")
        assert "qwen3" in lmstudio_ref_for_key("qwen3-8b-q8").lower()

    def test_picker_keys_returns_six(self) -> None:
        keys = picker_keys()
        assert len(keys) == 6
        assert DEFAULT_CATALOG_ID in keys

    def test_entries_for_provider_returns_all_six(self) -> None:
        specs = entries_for_provider("ollama")
        assert len(specs) == 6
        specs = entries_for_provider("lmstudio")
        assert len(specs) == 6

    def test_resolve_for_provider_warns_on_ollama_tag_fallback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        spec = resolve_for_provider("nomic-v1.5-q4", "ollama")

        assert spec.key == "nomic-v1.5-q4"
        assert "Ollama will use its tag fallback" in caplog.text

    def test_catalog_summary_returns_all_entries(self) -> None:
        summary = catalog_summary()
        assert len(summary) == len(all_keys())
        for entry in summary:
            assert "key" in entry
            assert "dim" in entry
            assert "family" in entry


class TestCatalogSerialization:
    """Test to_dict serialization."""

    def test_to_dict_roundtrip(self) -> None:
        spec = get_spec("qwen3-8b-q8")
        assert spec is not None
        d = spec.to_dict()
        assert d["key"] == "qwen3-8b-q8"
        assert d["dim"] == 4096
        assert d["family"] == "qwen3"
        assert d["ollama_tag"] == "qwen3-embedding:8b-q8_0"
        assert d["ollama_quant_real"] is True
        assert d["compatibility"]["lmstudio"] == "experimental"
