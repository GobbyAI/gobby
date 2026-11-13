# Embedding models: options for decision D4 (revised 2026-09-24)

**Status: options only. No pilot is authorized.** The PD's call on D4 is to wait until the 0.6 golden path is done. After that, the deciding test is one run of the eval set in §8, which yields a yes or no.

This revision follows Josh's feedback, relayed by #14069 on 2026-09-24:
- Keep Qwen3-Embedding-0.6B and rank it head-to-head against the other candidates.
- Compare every model at its native dimension, assuming a full re-index of all three collections. 768 truncation is no longer a goal.
- Serve through Ollama.
- Add an MLX section, narrowed to 8bit against mxfp8.

Everything here comes from web and doc research plus a reading of Gobby's code. No model was downloaded, loaded or served.

**How to read the scores.** Each score carries a tag:
- **[V]**: run by the vendor, including self-submitted leaderboard entries.
- **[I]**: run by an independent third party.
- **[I\*]**: a third party that was an NVIDIA launch partner.
- **[IBM]**: IBM's Granite R2 paper reporting a competitor's score. The paper doesn't say whether IBM re-ran the model.

Bracketed IDs point to the source list at the end. Every source was accessed on 2026-09-24. "Not verified" means no fetched page confirmed the claim.

## 1. Options at a glance

### Memories and MCP tools (general text, about 4.5k vectors)

| Option | Base / license | Native dim | Ollama | Gobby config gaps | Independent | Vendor |
|---|---|---|---|---|---|---|
| **Qwen3-Embedding-0.6B** | Qwen3 / Apache-2.0 | 1024 | official library, `qwen3-embedding:0.6b` (q8_0, fp16) [R42] | **None.** Catalog entries `qwen3-0.6b-q8` and `qwen3-0.6b-f16` exist with the query instruction | AIMultiple 0.817 [T39]; Mem0 LongMemEval 78.71 [I\*][R14] | Eng v2 70.70, MMTEB 64.33 [R43] |
| **Harrier-oss-v1-0.6b** | Qwen3 architecture (`Qwen3Model` in config.json) / MIT [R2] | 1024 | one community upload [R8] | Needs a catalog entry. The query instruction fits `query_prefix` | **AIMultiple 0.891**, the best commercially usable model in that test [T39] | MMTEB 69.0 [T1] |
| **EmbeddingGemma-300m** | Gemma 3 / Gemma terms, gated [R23] | 768 | official library, `embeddinggemma` [R42] | Needs a catalog entry and a **document prefix** (`title: none \| text: `). Context is capped at 2K tokens | AIMultiple 0.871 [T39]; Zep facts 0.645 [I\*][R18] | Eng v2 69.67 [R22] |
| **Nemotron-3-Embed-1B** | Ministral 3 / OpenMDW-1.1 [R12] | 2048 | **none** | Needs a catalog entry, a **document prefix** (`passage: `) and a server that runs it bidirectionally. That last part is not verified for GGUF | Zep facts 0.663 [I\*][R18]; Mem0 LongMemEval 80.38 [I\*][R14] | RTEB 72.38, MMTEB retrieval 71.04 [R12] |
| granite-embedding-english-r2 | ModernBERT / Apache-2.0 | 768 | **none**; Ollama has no ModernBERT support [R29] | Needs a catalog entry; no prompts | AIMultiple 0.818 [T39] | MTEB v2 62.8 [R28] |
| nomic-embed-text-v1.5 (today) | nomic-bert / Apache-2.0 | 768 | library | none | none found | MTEB v1 62.28 [T9] |

**Reading:**
- **The independent evidence is thin and it splits.**
  - AIMultiple (551 queries; updated 2026-09-23) ranks Harrier-0.6b first, then EmbeddingGemma, then granite-r2 and Qwen3-0.6B, which tie.
  - Its own ordering is inconsistent: Harrier's 0.8911 is listed at #4 below two lower scores [R6].
  - The memory tests by Zep and Mem0 are partner-run. They put Nemotron-1B 1–2 points ahead of EmbeddingGemma and Qwen3-0.6B.
- **Under Ollama, only Qwen3-0.6B and EmbeddingGemma run from the official library today.**
  - Harrier-0.6b depends on community GGUF builds.
  - Nemotron and granite-r2 have no Ollama path at all.
- **Other Qwen3-based models,** for reference only:
  - pplx-embed-v1-0.6b: MIT, 1024 dimensions, no prompts [R20].
  - voyage-4-nano: Apache-2.0, 2048 dimensions, needs query and document prompts [R21].
  - Neither has an independent score or an official Ollama build.

### gcode code index (444k vectors; natural-language query to code)

| Option | Independent NL-to-code | Vendor code score | Context | Ollama |
|---|---|---|---|---|
| **Qwen3-Embedding-0.6B** | **CORE-Bench nDCG@10: L1 66.9, L2 17.0, L3 32.6** [C25] | MTEB-Code 75.41 [R43] | 32K | library |
| EmbeddingGemma-300m | none. A niche 1C:Enterprise set with mostly Russian queries scores it 0.540, the best off-the-shelf model there [R10] | MTEB Code 68.76 [R22] | **2K** | library |
| granite-311m-multilingual-r2 | 1C 0.485 [R10] | MTEB Code 63.8 [R30] | 32K | none |
| Harrier-0.6b | none | none | 32K | community |
| Harrier-270m (Gemma 3) | 1C 0.480 [R10] | MTEB Code 62.4 [IBM][R11] | 32K | none |
| CodeRankEmbed (137M) | CORE-Bench L1 47.4, L2 12.1, L3 22.5 [C25] | CoIR 60.1 [R36] | 8K | community |
| Nemotron-3-Embed-1B | none. CoIR datasets are in its training data [R12] | none | 32K | none |
| *Reference: other Qwen-based code models* | C2LLM-7B L1 72.4 (Apache-2.0); jina-code-1.5b 56.2 (CC BY-NC) [C25] | | | |

**Reading:**
- **Qwen3-0.6B is the only option with an independent NL-to-code result.** It also leads every code measure found. At L1 it scores 19.5 nDCG points above CodeRankEmbed, the only other option tested.
- **CORE-Bench levels.** L1 is traditional code search, the closest to gcode's queries. L2 and L3 are agentic settings, where every model drops sharply.
- **EmbeddingGemma's 2K-token cap** would truncate long symbol chunks.
- **Identifier queries defeat every dense model:** CoREB measured 0.000–0.015 nDCG@10 [C26]. gcode's BM25 lane stays essential whichever model is chosen.

### One model or two
- **One model.** A single model means one Ollama model resident and one dimension. Qwen3-0.6B is the only option with evidence on both workloads.
- **Two models.** For example, EmbeddingGemma for memories and Qwen3-0.6B for code. That would need a code change to split `ai.embeddings.dim`, which gcode and memories share today. It would also put a second model on the GPU; memory a5ad8b5c measured one LM Studio instance already using it fully.

## 2. Fit with Gobby's embedding config (read from code on 2026-09-24)

- **A switch needs a catalog entry.**
  - `embedding_switch.py:283` calls `get_spec_or_raise(catalog_key)`.
  - `src/gobby/ai/embedding_catalog.py` lists only nomic v1.5 (768) and Qwen3 0.6B / 4B / 8B (1024 / 2560 / 4096).
  - Each entry pins a GGUF repo, filename and sha256, plus an LM Studio ref and an Ollama tag.
  - **Qwen3-0.6B needs no new code.** Every other option needs a new entry. Pinning a community GGUF's sha256 is also a trust decision.
- **Query prefix.** Each entry has a `query_prefix`. It covers the query instructions of Qwen3, Harrier and CodeRankEmbed.
- **Document prefix: missing.** `_apply_prefix` (`src/gobby/ai/embeddings.py:230`) prefixes documents only for nomic, chosen by model name. EmbeddingGemma, Nemotron and voyage-4-nano need this support.
- **Dimension.**
  - The catalog records each model's native dimension. The switch flow rebuilds all three collections behind aliases (memory b281f9e6).
  - There is no truncation code, and none is needed at native size.
  - At float32 the gcode index grows from 1.36 GB (768) to 1.82 GB (1024) or 3.64 GB (2048).
- **gcode queries now arrive in query mode.** #22827 landed as merge cfe241bc92, and gcode was promoted on 2026-09-24. Query instructions now apply on the daemon route.
- **The embeddings timeout is a fixed 10 s.** `EMBEDDINGS_TIMEOUT` is set in `crates/gcore/src/ai/mod.rs:25`. Larger models on bulk index batches could exceed it.

## 3. Serving through Ollama

- **Ollama serves embedding models only on its GGUF (GGML) engine.**
  - Its MLX engine (a preview since 0.19, 2026-03-30) runs chat models [M8].
  - It doesn't serve `/api/embed`. Issue #16076 (open since 2026-05-10) shows Qwen3-Embedding on MLX returning HTTP 500, and nomic-v2-moe rejected as "unsupported architecture" [M9].
  - Release notes through v0.34.4 (2026-09-23) don't mention embeddings [M10].
- **Qwen3-0.6B under Ollama** comes as `0.6b-q8_0`, which is also the default `0.6b` tag (639 MB), or as `0.6b-fp16` (1.2 GB) [M11].
- **End token.**
  - Qwen3 pools the last token and needs `<|endoftext|>` appended.
  - A user reports that Ollama's build handles this correctly [L3].
  - The official Qwen GGUF, which Gobby's catalog pins for LM Studio, predates llama.cpp's fix of 2025-08-02 [L6][L9]. That problem affects only the LM Studio path.
  - Harrier-0.6b also pools the last token. Whether its community GGUFs append the end token is **not verified**.
- **M5 Neural Accelerators.**
  - llama.cpp turned on the Metal 4 tensor API for M5 by default in PR #16634 (merged 2025-11-06). It measured about 2× faster prompt processing [M3].
  - Embedding is pure prompt processing, so this matters.
  - Whether Ollama's GGML engine has it turned on is **not verified**. Ollama's announcement credits the accelerators only to its MLX engine [M8].
- **LM Studio, today's server.**
  - Bug #2040 (open since 2026-06-11) reports runtime 2.21.0 failing the tensor-API check on M5. It lost 2.09–2.44× in prefill [M4].
  - This machine has runtimes 2.31.2 through 2.41.0 installed. Whether those fix it is **not verified**.
  - That may affect today's nomic throughput too.
- **Check before any switch.** Ollama's embed API truncates inputs longer than the context by default (`truncate` defaults to true) [L44]. The effective context of each model under Ollama is **not verified**.

## 4. MLX (Qwen3-Embedding-0.6B at native 1024)

Neither of Gobby's current servers can serve an MLX embedding model:
- **LM Studio's MLX engine: no.** LM Studio doesn't recognize MLX embedding models, while the GGUF of the same model works (bug #808, open since 2025-07-29) [R45]. No LM Studio changelog through 0.4.25 (2026-09-19) mentions a fix [R45].
- **Ollama's MLX engine: no.** It doesn't serve `/api/embed` (§3).

MLX therefore means running a separate server. This section applies only if one is added.

**Q1. Which MLX servers expose `/v1/embeddings` for Qwen3-Embedding-0.6B?**

| Server | Qwen3-Embedding-0.6B on `/v1/embeddings` | Maturity (release dates from the GitHub API) | Published Apple Silicon embedding throughput |
|---|---|---|---|
| **vllm-metal** (vLLM project's Apple Silicon plugin) | **Yes, experimental.** The docs list `mlx-community/Qwen3-Embedding-0.6B-8bit` as tested, with LAST pooling and L2 normalization. Run it with `VLLM_METAL_USE_PAGED_ATTENTION=1 vllm serve <model> --runner pooling` [M12]. Official Qwen checkpoints load since v0.29.0 (PR #736), with Matryoshka dimensions supported [M13][M14]. mxfp8 is not listed as tested | v0.30.0 on 2026-09-23, plus a dev build on 2026-09-24. v0.29.0 was 2026-09-11. 1.8k stars, Apache-2.0 [M15] | None. The v0.28.0 post (2026-09-22) charts only text generation. On M5 it uses the Neural Accelerators ("NAX") for prefill [M16] |
| **vllm-mlx** (waybarrios) | Endpoint exists since v0.2.6, built on mlx-embeddings [R48]. Its README example uses MiniLM. Qwen3-Embedding is not documented. Pooling would come from mlx-embeddings' last-token `qwen3.py` [R46]; **not verified** end to end | v0.5.0 on 2026-09-17. 1.6k stars, Apache-2.0 [M15] | None |
| **oMLX** (jundot) | **Not today.** Embeddings go through mlx-embeddings for BERT, BGE-M3 and ModernBERT. Text Qwen3-Embedding support with last-token pooling is an open feature request (#2350, 2026-07-23). "Broken embeddings?" (#3731, 2026-09-18) is also open [M17] | Stable v0.6.4 on 2026-08-29; dev v0.7.0.dev4 on 2026-09-18. 22.2k stars, Apache-2.0. A macOS menu-bar app plus server that serves several models [M15] | None |
| mlx-openai-server, mlx-omni-server | Claimed; **not verified** | – | – |

- **Query instruction.** No server applies it. Gobby adds it on the client through the catalog's `query_prefix`.
- **Connecting Gobby.** Gobby already has a `vllm` embedding provider, which resolves the served model ID from the server's `/v1/models` (`catalog_model_for_provider`, `embedding_catalog.py`). The existing `qwen3-0.6b-*` catalog entries supply the 1024 dimension and the query prefix. Pointing that provider at vllm-metal needs **no new code, but it is not verified** end to end.
- **Running alongside LM Studio.** Each server is a separate process on its own port, with its own copy of its model's weights; none shares a model LM Studio has loaded.
  - Serving Qwen3-0.6B next to LM Studio's nomic does not duplicate the daemon's model, which is the case memory a5ad8b5c forbids.
  - It does put a second embedding model on the same GPU. The two would compete during bulk indexing, and that contention is unmeasured.
  - Memory is not the constraint: the 8bit build of the 0.6B is well under 1 GB.

**Q2. Pooling and the end token: correct.**
- mlx-embeddings' `qwen3.py` pools the last token and handles padding on either side [R46].
- All three mlx-community builds (4bit-DWQ, 8bit, mxfp8) ship a `tokenizer.json` whose post-processor appends `<|endoftext|>`, identical to Qwen's own repo. I checked the files on 2026-09-24.
- That post-processor comes from Qwen's HF PR #2, merged 2025-06-06 [M1].
- So the GGUF end-token bug does not apply. End-to-end parity with sentence-transformers is **not verified**.

**Q3. Quality, 8bit against mxfp8: no measurement exists** for this model or for any embedding model.
- The formats differ [M5]:
  - 8bit is affine int8 with a group size of 64 and a float scale plus bias per group.
  - mxfp8 uses E4M3 elements with a group size of 32 and a power-of-two (E8M0) scale, with no bias.
- One systematic study found MXINT8 "superior to its FP counterpart in both algorithmic accuracy and hardware efficiency" [M7]. MLX's 8bit is not MXINT8, so that result transfers only loosely.
- **The one data point found for this model** comes from a vllm-metal issue:
  - The reporter compared Metal output against 56,523 stored vectors that CUDA vLLM had built in bf16.
  - The official weights on Metal reached a median cosine of 0.99983, with 0.99972 in the worst case.
  - The mlx-community 8bit build showed "~2e-3 cosine drift" from the original weights [M14].
  - No such figure exists for mxfp8.
- **Lean: 8bit**, until the eval set measures both.

**Q4. Speed on an M5 Max: no published MLX-versus-GGUF embedding figure.**
- Apple measured 3.33–4.06× faster time to first token on M5 than on M4 with MLX [M2]. That is prefill, the same computation as embedding.
- Apple's test covered bf16, 4-bit and MXFP4, but not 8bit or mxfp8. Whether mxfp8 matrix multiplies use the Neural Accelerators is not stated.
- The only figure for the 0.6B is 44K tokens/s for 4bit-DWQ on an M2 Max [L47].

**4bit-DWQ** is only a fallback if both 8-bit formats prove too slow. It has no quality figure either.

## 5. Throughput on Apple Silicon

| Model | Setup | Throughput | Source |
|---|---|---|---|
| Qwen3-Emb 0.6B / 4B / 8B | MLX 4-bit DWQ, M2 Max, batch 32 | 44K / 18K / 11K tokens/s | [L47] |
| Qwen3-Emb-0.6B | llama.cpp, platform not stated | 6,878 tokens/s | [L48] (2026-02-26) |
| nomic-v1.5 f16 (today) | LM Studio, M5 Max | 70–110 texts/s, about 18–29K tokens/s | memory a5ad8b5c |

- No Apple Silicon figure was found for EmbeddingGemma, Harrier, Nemotron or granite.
- A 2026-06-23 blog claims M5 Max figures of about 10M tokens/s, which is implausible. It is ignored [L50].

## 6. Licenses

A non-commercial license restricts *use*, including running the model. The permissive licenses below impose obligations only when the model is redistributed.

| Commercially usable | Excluded |
|---|---|
| Qwen3-Embedding, granite r2, voyage-4-nano: Apache-2.0 [L13][R28][R21] | jina v5 and jina-code: CC BY-NC 4.0 [L42][L43] |
| Harrier, pplx-embed, CodeRankEmbed: MIT [T1][R20][R36] | llama-embed-nemotron-8b: NVIDIA non-commercial [T27] |
| Nemotron-3-Embed: OpenMDW-1.1. Use is unrestricted; redistributing requires keeping the license and notices; the grant ends on a patent suit [R15] | SFR-Embedding-2_R, SFR-Embedding-Code, NV-Embed-v2, Linq-Embed-Mistral: CC BY-NC [T39][C6][T28][T29] |
| EmbeddingGemma: Gemma terms (2026-04-01). Commercial use is allowed. The model is gated, and Google may restrict use it believes breaks the terms [R24] | KaLM-12B: "not intended for use within the European Union" [T6] |

## 7. What changed since mid-2025; is the leaderboard gamed?

**Releases:**
- 2025: EmbeddingGemma (2025-09) and C2LLM (2025-12).
- 2026: voyage-4-nano (2026-01), jina v5 and pplx-embed (2026-02), Harrier (2026-03), granite multilingual r2 (2026-04), and Nemotron 3 Embed (2026-07-16).
- No Qwen3.5 or Qwen4 embedding model was found.

**Gaming and saturation:**
- MTEB flags models trained on its evaluation datasets. The top two MMTEB models are only 73–78% zero-shot [T5][T41].
- RTEB's private column is the part the maintainers run themselves. It was pulled on 2026-01-14, and whether it has been restored is **not verified** [R19]. NVIDIA's RTEB claims may therefore cover only the public datasets.
- Under adversarial prompt selection, "any model can be promoted to first place" [T43].
- MTEB-Code:
  - C2LLM trained on the evaluation datasets' training splits [C10].
  - Nemotron trained on CoIR datasets [R12].
  - About 51% of CoSQA is mislabeled [C26].
- **Conclusion:** vendor averages no longer separate the top models. Gobby's own queries (§8) are the test that decides.

## 8. The deciding test (prepared; no model served)

**Eval set:** `.gobby/plans/research/embedding-eval-set-2026-09.md`. It holds 100 real queries with expected hits, drawn from session history between 2026-08-19 and 2026-09-24: 44 memory queries, 12 tool queries and 44 gcode queries (7 of them identifier controls). The JSONL sits in a fenced block; that file gives the method, the caveats and a one-line extraction command.

**The run, after the golden path:**
- Embed the eval corpus with each option on the same Ollama server, in a quiet window.
- Load one model at a time, never alongside the daemon's live model (memory a5ad8b5c).
- Score recall@k and nDCG@10 against nomic.

## Corrections to the first version

- "No code change needed" for 768 truncation was wrong: Gobby has no truncation code. The point is moot now that the comparison uses native dimensions.
- "CORE-Bench is by Qwen3-Embedding authors" was wrong. Its authors are at Beihang University and Peking University [C25].
- Harrier-0.6b is built on the Qwen3 architecture [R2].
- The research digest's CORE-Bench figures of 38.8 and 27.3 are averages over L1–L3. This report gives each level.

## Sources

All sources were accessed on 2026-09-24. Page dates are shown where the page gives one.

**General text**
- T1: huggingface.co/microsoft/harrier-oss-v1-0.6b
- T5: the-decoder.com/microsofts-bing-team-open-sources-harrier-embedding-model/ (2026-04-07)
- T6: huggingface.co/tencent/KaLM-Embedding-Gemma3-12B-2511 (and LICENSE.txt)
- T9: huggingface.co/nomic-ai/nomic-embed-text-v1.5
- T27: huggingface.co/nvidia/llama-embed-nemotron-8b
- T28: huggingface.co/nvidia/NV-Embed-v2
- T29: huggingface.co/Linq-AI-Research/Linq-Embed-Mistral
- T39: aimultiple.com/open-source-embedding-models (published 2026-08-10, updated 2026-09-23)
- T41: github.com/embeddings-benchmark/mteb/discussions/2351 (2025-01-11)
- T43: arxiv.org/abs/2605.22544 (2026-05-21, revised 2026-08-13)

**Code**
- C6: huggingface.co/Salesforce/SFR-Embedding-Code-2B_R
- C10: arxiv.org/html/2512.21332 (C2LLM; 2025-12)
- C25: arxiv.org/html/2606.11864v3 (CORE-Bench; Beihang and PKU; v1 2026-06-10, v3 2026-08-24; Table 3 NDCG@10/Recall@100)
- C26: arxiv.org/html/2605.04615 (CoREB; 2026-05-06)

**Local fit**
- L3: huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/discussions/17 (2025-11-21)
- L6: github.com/ggml-org/llama.cpp/pull/15023 (merged 2025-08-02)
- L9: HF API for Qwen/Qwen3-Embedding-0.6B-GGUF (lastModified 2025-07-14)
- L13: huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF
- L42: huggingface.co/jinaai/jina-embeddings-v5-text-small-retrieval-GGUF
- L43: huggingface.co/jinaai/jina-code-embeddings-0.5b
- L44: docs.ollama.com/api/embed
- L47: github.com/jakedahn/qwen3-embeddings-mlx
- L48: github.com/ggml-org/llama.cpp/issues/19933 (2026-02-26)
- L50: contracollective.com/blog/local-embeddings-apple-silicon-nomic-bge-qwen3-m5-max-2026 (2026-06-23; disregarded)

**Non-Qwen candidates**
- R2: huggingface.co/microsoft/harrier-oss-v1-0.6b/raw/main/config.json
- R6: aimultiple.com/open-source-embedding-models (updated 2026-09-23)
- R8: ollama.com/search?q=harrier
- R10: arxiv.org/html/2608.19957v1 (1C:Enterprise code retrieval; 2026-08-21)
- R11: arxiv.org/html/2605.13521v2 (Granite Multilingual R2; 2026-05-14)
- R12: huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16 (2026-07-16)
- R14: huggingface.co/blog/nvidia/nemotron-3-embed-wins-rteb (2026-07-16)
- R15: openmdw.ai/license/1-1/ (updated 2026-05-27)
- R18: blog.getzep.com/evaluating-nemotron-3-embed-for-agent-memory/ (2026-07-16)
- R19: github.com/embeddings-benchmark/mteb/issues/3934 (2026-01-14)
- R20: huggingface.co/perplexity-ai/pplx-embed-v1-0.6b; arxiv.org/abs/2602.11151 (2026-02-11)
- R21: huggingface.co/voyageai/voyage-4-nano
- R22: ai.google.dev/gemma/docs/embeddinggemma/model_card (updated 2025-09-25)
- R23: huggingface.co/api/models/google/embeddinggemma-300m (lastModified 2025-09-25)
- R24: ai.google.dev/gemma/terms (updated 2026-04-01)
- R28: huggingface.co/ibm-granite/granite-embedding-english-r2 (2025-08-15)
- R29: huggingface.co/ibm-granite/granite-embedding-english-r2/discussions/2 (2025-09-11; update 2026-02-06)
- R30: huggingface.co/ibm-granite/granite-embedding-311m-multilingual-r2 (2026-04-29)
- R36: huggingface.co/nomic-ai/CodeRankEmbed
- R42: ollama.com/search?c=embedding
- R43: huggingface.co/Qwen/Qwen3-Embedding-0.6B
- R45: github.com/lmstudio-ai/lmstudio-bug-tracker/issues/808 (2025-07-29); lmstudio.ai/changelog (2026-06-08 to 2026-09-19)
- R46: github.com/Blaizzy/mlx-embeddings (models/qwen3.py)
- R48: github.com/waybarrios/vllm-mlx/blob/main/docs/guides/embeddings.md

**MLX, M5 and Ollama**
- M1: huggingface.co/Qwen/Qwen3-Embedding-0.6B/discussions/2 (opened 2025-06-05, merged 2025-06-06). Plus the tokenizer.json of Qwen/Qwen3-Embedding-0.6B and mlx-community/Qwen3-Embedding-0.6B-{4bit-DWQ,8bit,mxfp8}, checked 2026-09-24
- M2: machinelearning.apple.com/research/exploring-llms-mlx-m5 (2025-11-19)
- M3: github.com/ggml-org/llama.cpp/pull/16634 (opened 2025-10-17, merged 2025-11-06)
- M4: github.com/lmstudio-ai/lmstudio-bug-tracker/issues/2040 (2026-06-11, open)
- M5: ml-explore.github.io/mlx, mlx.core.quantize (MLX 0.32.2 docs); github.com/ml-explore/mlx/pull/2688 (mxfp8, merged 2025-10-28)
- M7: arxiv.org/abs/2510.25602 ("INT v.s. FP"; 2025-10-29)
- M8: ollama.com/blog/mlx (2026-03-30)
- M9: github.com/ollama/ollama/issues/16076 (2026-05-10, open)
- M10: github.com/ollama/ollama/releases (v0.34.4, 2026-09-23)
- M11: ollama.com/library/qwen3-embedding/tags
- M12: docs.vllm.ai/projects/vllm-metal/en/latest/text_embedding_pooling/ (latest docs)
- M13: github.com/vllm-project/vllm-metal/releases (v0.29.0 2026-09-11, v0.30.0 2026-09-23)
- M14: github.com/vllm-project/vllm-metal/issues/730 (2026-09-07; closed by PR #736)
- M15: GitHub API, api.github.com/repos/{vllm-project/vllm-metal, waybarrios/vllm-mlx, jundot/omlx} and their releases (queried 2026-09-24)
- M16: vllm.ai/blog/2026-09-22-vllm-metal-v0-28-0 (2026-09-22)
- M17: github.com/jundot/omlx README; issues #2350 (2026-07-23) and #3731 (2026-09-18)
