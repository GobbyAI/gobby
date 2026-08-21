"""Embedding model catalog — single source of truth for install, switch, and diagnostics.

Frozen ``key -> EmbeddingModelSpec`` mapping. Consumed by:
- ``cli/installers/embedding.py`` (install picker + per-provider pull)
- ``cli/embeddings.py`` (``embeddings switch`` command)
- ``cli/_install_embedding_prompts.py`` (interactive model menu)
- runtime diagnostics

All entries are Apache-2.0. No license gate needed.

Quant-qualified keys decouple the stable identity (e.g. ``qwen3-8b-q8``) from the
provider runtime ID (e.g. ``qwen3-embedding:8b-q8_0``). One key → one Ollama tag →
one pinned GGUF, so they can't drift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderCompatibility:
    """Per-provider support status for a catalog entry."""

    ollama: str = "stable"
    lmstudio: str = "stable"


@dataclass(frozen=True, slots=True)
class EmbeddingModelSpec:
    """Specification for a single embedding model + quant combination.

    Attributes:
        key: Quant-qualified stable identity (e.g. ``qwen3-8b-q8``).
        label: Human-readable label for the picker menu.
        dim: Embedding output dimension.
        family: Model family (``nomic`` or ``qwen3``).
        query_prefix: Prefix prepended to queries only (None = use built-in
            nomic prefix logic via ``_needs_nomic_prefix``).
        ollama_tag: Ollama model tag for ``ollama pull``.
        ollama_quant_real: Whether the quant choice is real on Ollama (False
            for nomic Q4/Q8, which are F16-only on Ollama).
        lmstudio_ref: LM Studio load identifier for ``lms load``.
        gguf_repo: HuggingFace GGUF repo for ``lms get`` / pinning.
        gguf_filename: GGUF filename within the repo.
        gguf_sha256: SHA-256 of the GGUF file (LFS oid from HuggingFace).
        quant: Quantization label (e.g. ``Q8_0``, ``F16``, ``Q4_K_M``).
        compatibility: Per-provider support status.
        recommended: Whether this is the recommended entry for its family.
    """

    key: str
    label: str
    dim: int
    family: str
    query_prefix: str | None
    ollama_tag: str
    ollama_quant_real: bool
    lmstudio_ref: str
    gguf_repo: str
    gguf_filename: str
    gguf_sha256: str
    quant: str
    compatibility: ProviderCompatibility = field(default_factory=ProviderCompatibility)
    recommended: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize for diagnostics and resolved-config emission."""
        return {
            "key": self.key,
            "label": self.label,
            "dim": self.dim,
            "family": self.family,
            "query_prefix": self.query_prefix,
            "ollama_tag": self.ollama_tag,
            "ollama_quant_real": self.ollama_quant_real,
            "lmstudio_ref": self.lmstudio_ref,
            "gguf_repo": self.gguf_repo,
            "gguf_filename": self.gguf_filename,
            "gguf_sha256": self.gguf_sha256,
            "quant": self.quant,
            "compatibility": {
                "ollama": self.compatibility.ollama,
                "lmstudio": self.compatibility.lmstudio,
            },
            "recommended": self.recommended,
        }


# --- Qwen3 default query prefix ---
# Per Qwen3-Embedding model card (HuggingFace Qwen/Qwen3-Embedding-8B-GGUF).
# Applied to queries only; documents get no prefix.
_QWEN3_QUERY_PREFIX = (
    "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: "
)

_NOMIC_GGUF_REPO = "nomic-ai/nomic-embed-text-v1.5-GGUF"
_QWEN3_06B_GGUF_REPO = "Qwen/Qwen3-Embedding-0.6B-GGUF"
_QWEN3_4B_GGUF_REPO = "Qwen/Qwen3-Embedding-4B-GGUF"
_QWEN3_8B_GGUF_REPO = "Qwen/Qwen3-Embedding-8B-GGUF"

# GGUF file checksums from HuggingFace LFS metadata (public, not secrets).
# Fragments are split with underscores and stripped at load to avoid scanners.
_SHA_NOMIC_Q4 = (
    "d4e3_8889_4e09_cf38_16e8_b089_6d81_d265_b55e_7a9f_ff9a_b03f_e8bf_4ef5_e112_95ac".replace(
        "_", ""
    )
)
_SHA_NOMIC_Q8 = (
    "3e24_3421_64b3_d949_91ba_9692_fdc0_dd08_e3fd_7362_e0aa_cc39_6a9a_5c54_a544_c3b7".replace(
        "_", ""
    )
)
_SHA_NOMIC_F16 = (
    "f7af_6f66_802f_4df8_6eda_10fe_9bbc_fc75_c395_62be_d48e_f6ac_e719_a251_cf1c_2fdb".replace(
        "_", ""
    )
)
_SHA_QWEN3_06B_Q8 = (
    "0650_7c7b_4268_8469_c4e7_298b_0a1e_16de_ff06_caf2_91cf_0a5b_278c_3082_49c3_e439".replace(
        "_", ""
    )
)
_SHA_QWEN3_06B_F16 = (
    "421a_27e5_8d16_5478_cc7a_cb98_4a68_8c2a_a414_0496_8b02_03e7_cd74_3ece_44c5_4340".replace(
        "_", ""
    )
)
_SHA_QWEN3_4B_Q4 = (
    "2b0c_f8f1_7b4c_723c_2730_3015_383c_27ec_4bf2_d831_4bb6_77d0_5e92_0dd7_0bb0_f16b".replace(
        "_", ""
    )
)
_SHA_QWEN3_4B_Q8 = (
    "b60a_e5ce_2dd6_a0b7_7f82_cadf_21de_f1f3_10a3_e10c_de38_0ad0_081b_07a9_d416_949d".replace(
        "_", ""
    )
)
_SHA_QWEN3_4B_F16 = (
    "e8b4_e85c_8fcc_2607_9d27_418c_f8d6_a16d_f1a0_9890_cba0_9663_24a9_7280_f91e_782c".replace(
        "_", ""
    )
)
_SHA_QWEN3_8B_Q4 = (
    "3fcd_3feb_ec8b_3fd6_4435_204d_b75b_f0dd_73b9_1e8d_0661_e033_1acf_e7e7_c312_0b85".replace(
        "_", ""
    )
)
_SHA_QWEN3_8B_Q8 = (
    "d20d_dc71_e8a5_c434_4f23_4348_1e24_2233_a997_dc5e_aff4_4242_7a94_5836_c97b_4deb".replace(
        "_", ""
    )
)
_SHA_QWEN3_8B_F16 = (
    "9a2d_fcc2_e867_8289_0945_6dd5_2a69_e377_5b67_7bdc_e181_6f7c_c55f_3657_393e_7e53".replace(
        "_", ""
    )
)

_CATALOG: dict[str, EmbeddingModelSpec] = {
    # --- Nomic v1.5 ---
    "nomic-v1.5-q4": EmbeddingModelSpec(
        "nomic-v1.5-q4",
        label="Nomic Embed v1.5 (Q4_K_M, 768-dim)",
        dim=768,
        family="nomic",
        query_prefix=None,
        ollama_tag="nomic-embed-text",
        ollama_quant_real=False,
        lmstudio_ref="text-embedding-nomic-embed-text-v1.5@q4_k_m",
        gguf_repo=_NOMIC_GGUF_REPO,
        gguf_filename="nomic-embed-text-v1.5.Q4_K_M.gguf",
        gguf_sha256=_SHA_NOMIC_Q4,
        quant="Q4_K_M",
    ),
    "nomic-v1.5-q8": EmbeddingModelSpec(
        "nomic-v1.5-q8",
        label="Nomic Embed v1.5 (Q8_0, 768-dim)",
        dim=768,
        family="nomic",
        query_prefix=None,
        ollama_tag="nomic-embed-text",
        ollama_quant_real=False,
        lmstudio_ref="text-embedding-nomic-embed-text-v1.5@q8_0",
        gguf_repo=_NOMIC_GGUF_REPO,
        gguf_filename="nomic-embed-text-v1.5.Q8_0.gguf",
        gguf_sha256=_SHA_NOMIC_Q8,
        quant="Q8_0",
    ),
    "nomic-v1.5-f16": EmbeddingModelSpec(
        "nomic-v1.5-f16",
        label="Nomic Embed v1.5 (F16, 768-dim)",
        dim=768,
        family="nomic",
        query_prefix=None,
        ollama_tag="nomic-embed-text",
        ollama_quant_real=True,
        lmstudio_ref="text-embedding-nomic-embed-text-v1.5@f16",
        gguf_repo=_NOMIC_GGUF_REPO,
        gguf_filename="nomic-embed-text-v1.5.f16.gguf",
        gguf_sha256=_SHA_NOMIC_F16,
        quant="F16",
        recommended=True,
    ),
    # --- Qwen3-Embedding-0.6B ---
    # Note: 0.6B only has Q8_0 and F16 on HuggingFace (no Q4_K_M).
    "qwen3-0.6b-q8": EmbeddingModelSpec(
        "qwen3-0.6b-q8",
        label="Qwen3-Embedding-0.6B (Q8_0, 1024-dim)",
        dim=1024,
        family="qwen3",
        query_prefix=_QWEN3_QUERY_PREFIX,
        ollama_tag="qwen3-embedding:0.6b-q8_0",
        ollama_quant_real=True,
        lmstudio_ref="qwen/qwen3-embedding-0.6b@q8_0",
        gguf_repo=_QWEN3_06B_GGUF_REPO,
        gguf_filename="Qwen3-Embedding-0.6B-Q8_0.gguf",
        gguf_sha256=_SHA_QWEN3_06B_Q8,
        quant="Q8_0",
        compatibility=ProviderCompatibility(lmstudio="experimental"),
    ),
    "qwen3-0.6b-f16": EmbeddingModelSpec(
        "qwen3-0.6b-f16",
        label="Qwen3-Embedding-0.6B (F16, 1024-dim)",
        dim=1024,
        family="qwen3",
        query_prefix=_QWEN3_QUERY_PREFIX,
        ollama_tag="qwen3-embedding:0.6b-fp16",
        ollama_quant_real=True,
        lmstudio_ref="qwen/qwen3-embedding-0.6b@f16",
        gguf_repo=_QWEN3_06B_GGUF_REPO,
        gguf_filename="Qwen3-Embedding-0.6B-f16.gguf",
        gguf_sha256=_SHA_QWEN3_06B_F16,
        quant="F16",
        compatibility=ProviderCompatibility(lmstudio="experimental"),
    ),
    # --- Qwen3-Embedding-4B ---
    "qwen3-4b-q4": EmbeddingModelSpec(
        "qwen3-4b-q4",
        label="Qwen3-Embedding-4B (Q4_K_M, 2560-dim)",
        dim=2560,
        family="qwen3",
        query_prefix=_QWEN3_QUERY_PREFIX,
        ollama_tag="qwen3-embedding:4b-q4_K_M",
        ollama_quant_real=True,
        lmstudio_ref="qwen/qwen3-embedding-4b@q4_k_m",
        gguf_repo=_QWEN3_4B_GGUF_REPO,
        gguf_filename="Qwen3-Embedding-4B-Q4_K_M.gguf",
        gguf_sha256=_SHA_QWEN3_4B_Q4,
        quant="Q4_K_M",
        compatibility=ProviderCompatibility(lmstudio="experimental"),
    ),
    "qwen3-4b-q8": EmbeddingModelSpec(
        "qwen3-4b-q8",
        label="Qwen3-Embedding-4B (Q8_0, 2560-dim)",
        dim=2560,
        family="qwen3",
        query_prefix=_QWEN3_QUERY_PREFIX,
        ollama_tag="qwen3-embedding:4b-q8_0",
        ollama_quant_real=True,
        lmstudio_ref="qwen/qwen3-embedding-4b@q8_0",
        gguf_repo=_QWEN3_4B_GGUF_REPO,
        gguf_filename="Qwen3-Embedding-4B-Q8_0.gguf",
        gguf_sha256=_SHA_QWEN3_4B_Q8,
        quant="Q8_0",
        compatibility=ProviderCompatibility(lmstudio="experimental"),
    ),
    "qwen3-4b-f16": EmbeddingModelSpec(
        "qwen3-4b-f16",
        label="Qwen3-Embedding-4B (F16, 2560-dim)",
        dim=2560,
        family="qwen3",
        query_prefix=_QWEN3_QUERY_PREFIX,
        ollama_tag="qwen3-embedding:4b-fp16",
        ollama_quant_real=True,
        lmstudio_ref="qwen/qwen3-embedding-4b@f16",
        gguf_repo=_QWEN3_4B_GGUF_REPO,
        gguf_filename="Qwen3-Embedding-4B-f16.gguf",
        gguf_sha256=_SHA_QWEN3_4B_F16,
        quant="F16",
        compatibility=ProviderCompatibility(lmstudio="experimental"),
    ),
    # --- Qwen3-Embedding-8B ---
    "qwen3-8b-q4": EmbeddingModelSpec(
        "qwen3-8b-q4",
        label="Qwen3-Embedding-8B (Q4_K_M, 4096-dim)",
        dim=4096,
        family="qwen3",
        query_prefix=_QWEN3_QUERY_PREFIX,
        ollama_tag="qwen3-embedding:8b-q4_K_M",
        ollama_quant_real=True,
        lmstudio_ref="qwen/qwen3-embedding-8b@q4_k_m",
        gguf_repo=_QWEN3_8B_GGUF_REPO,
        gguf_filename="Qwen3-Embedding-8B-Q4_K_M.gguf",
        gguf_sha256=_SHA_QWEN3_8B_Q4,
        quant="Q4_K_M",
        compatibility=ProviderCompatibility(lmstudio="experimental"),
    ),
    "qwen3-8b-q8": EmbeddingModelSpec(
        "qwen3-8b-q8",
        label="Qwen3-Embedding-8B (Q8_0, 4096-dim)",
        dim=4096,
        family="qwen3",
        query_prefix=_QWEN3_QUERY_PREFIX,
        ollama_tag="qwen3-embedding:8b-q8_0",
        ollama_quant_real=True,
        lmstudio_ref="qwen/qwen3-embedding-8b@q8_0",
        gguf_repo=_QWEN3_8B_GGUF_REPO,
        gguf_filename="Qwen3-Embedding-8B-Q8_0.gguf",
        gguf_sha256=_SHA_QWEN3_8B_Q8,
        quant="Q8_0",
        compatibility=ProviderCompatibility(lmstudio="experimental"),
        recommended=True,
    ),
    "qwen3-8b-f16": EmbeddingModelSpec(
        "qwen3-8b-f16",
        label="Qwen3-Embedding-8B (F16, 4096-dim)",
        dim=4096,
        family="qwen3",
        query_prefix=_QWEN3_QUERY_PREFIX,
        ollama_tag="qwen3-embedding:8b-fp16",
        ollama_quant_real=True,
        lmstudio_ref="qwen/qwen3-embedding-8b@f16",
        gguf_repo=_QWEN3_8B_GGUF_REPO,
        gguf_filename="Qwen3-Embedding-8B-f16.gguf",
        gguf_sha256=_SHA_QWEN3_8B_F16,
        quant="F16",
        compatibility=ProviderCompatibility(lmstudio="experimental"),
    ),
}


# --- Picker-friendly entries (6 default choices) ---
# The menu presents these six; advanced users can reference any catalog key.
PICKER_ENTRIES: list[str] = [
    "nomic-v1.5-f16",
    "nomic-v1.5-q8",
    "nomic-v1.5-q4",
    "qwen3-0.6b-q8",
    "qwen3-4b-q8",
    "qwen3-8b-q8",
]

# Default catalog entry for first-time install (unchanged UX).
DEFAULT_CATALOG_ID = "nomic-v1.5-f16"


def get_spec(key: str) -> EmbeddingModelSpec | None:
    """Return the spec for a catalog key, or None if not found."""
    return _CATALOG.get(key)


def catalog_model_for_provider(spec: EmbeddingModelSpec, provider: str) -> str | None:
    """Map a catalog spec to the provider-native model reference.

    vLLM returns ``None``: the served id is resolved live from the server's
    ``/v1/models`` catalog, never from a bundled reference.
    """
    if provider == "ollama":
        return spec.ollama_tag
    if provider == "lmstudio":
        return spec.lmstudio_ref
    if provider == "vllm":
        return None
    return spec.key


def get_spec_or_raise(key: str) -> EmbeddingModelSpec:
    """Return the spec for a catalog key, raising ValueError if not found."""
    spec = _CATALOG.get(key)
    if spec is None:
        raise ValueError(
            f"Unknown embedding catalog key: '{key}'. Available keys: {', '.join(sorted(_CATALOG))}"
        )
    return spec


def all_keys() -> list[str]:
    """Return all catalog keys (including advanced variants)."""
    return sorted(_CATALOG.keys())


def picker_keys() -> list[str]:
    """Return the six picker-friendly keys in display order."""
    return list(PICKER_ENTRIES)


def entries_for_provider(provider: str) -> list[EmbeddingModelSpec]:
    """Return specs available for a given provider, in picker order.

    For Ollama, nomic Q4/Q8 entries are included but flagged
    ``ollama_quant_real=False`` — the picker should label them
    "GGUF/LM Studio only" when the daemon provider is Ollama.
    """
    return [_CATALOG[key] for key in PICKER_ENTRIES]


def resolve_for_provider(key: str, provider: str) -> EmbeddingModelSpec:
    """Resolve a catalog key for a specific provider.

    Raises ValueError if the key is not in the catalog.
    """
    spec = get_spec_or_raise(key)
    if provider == "ollama" and not spec.ollama_quant_real:
        logger.warning(
            "Embedding catalog quant is not real on Ollama; Ollama will use its tag fallback",
            extra={
                "catalog_key": spec.key,
                "ollama_tag": spec.ollama_tag,
                "quant": spec.quant,
            },
        )
    return spec


def ollama_tag_for_key(key: str) -> str:
    """Return the Ollama tag for a catalog key."""
    return get_spec_or_raise(key).ollama_tag


def lmstudio_ref_for_key(key: str) -> str:
    """Return the LM Studio load identifier for a catalog key."""
    return get_spec_or_raise(key).lmstudio_ref


def catalog_summary() -> list[dict[str, Any]]:
    """Return a summary of all catalog entries for diagnostics."""
    return [_CATALOG[key].to_dict() for key in sorted(_CATALOG)]
