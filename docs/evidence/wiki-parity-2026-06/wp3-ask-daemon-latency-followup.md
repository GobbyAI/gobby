# WP3 ask daemon latency follow-up (#779)

Date: 2026-06-15

Question tested: `ghook inbox enqueue fails`

## Measurements

| Probe | Route | Result |
|---|---:|---:|
| `gwiki ask --format json` | retrieval only | 0.31s |
| `gwiki ask --llm --ai daemon --require-ai --format json` | daemon synthesis | 17.55s |
| direct daemon `/api/llm/generate` request, one-sentence prompt, `feature_low`, `max_tokens:64` | daemon baseline | 9.12s |

The live `ask --ai daemon` run used model `gpt-5.4-mini`, 8 evidence excerpts,
16,230 evidence characters, and an estimated 4,323 prompt tokens. It was not
truncated and produced a cited, supported answer.

The saved WP3 artifact has the same shape: `wp3-qa-ghook-ask-daemon.json`
used `gpt-5.4-mini`, 8 evidence excerpts, 16,106 evidence characters, and an
estimated 4,296 prompt tokens. The companion direct-route artifact used the
same prompt shape and completed in 8.0s on LM Studio.

## Conclusion

The slow path is daemon/provider text generation, not local RRF, evidence
assembly, or citation checking. Retrieval-only completes in well under 1s, while
a minimal daemon generation call already consumes about 9s before the full RAG
prompt is involved.

Do not treat daemon `ask` as a <10s target while `feature_low` resolves to the
current Codex `gpt-5.4-mini` provider. Keep the <10s target for direct/local
routes, and use a <20s soft target for daemon-routed cited answers unless the
daemon profile changes to a lower-latency provider.
