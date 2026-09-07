# Code-wiki bakeoff provider compatibility

This report is the C0 provider and calibration gate for task **#21943** under epic **#21926**.
It follows the frozen [matrix](matrix.md) and accepted [environment](environment.md). Runtime
artifacts live under `/Users/josh/Projects/wiki-bakeoff-code-2026-09`; no full wiki generation is
authorized by this task.

## Implementation ledger

- [x] Capture one common synthetic evidence task through Codex subscription
  `gpt-5.6-terra`/medium and `gpt-5.6-luna`/medium.
- [x] Prove tool use, source attribution, persisted artifact, completion, exact requested/effective
  model and reasoning, timing, observable counters, and fallback behavior for both calibrations.
- [x] Probe every comparator's actual generation and embedding adapters without substituting a
  different provider, model, or reasoning level.
- [x] Run the managed Codex-operated `qwen/qwen3.8-27b`/xhigh bootstrap, tool-use, persisted-artifact,
  and completion probe with a 15-minute deadline.
- [x] Verify OpenDeepWiki container-to-LM-Studio native catalog and content requests; do not manage
  LM Studio model lifecycle.
- [x] Preserve one JSON Lines record per attempt, exact redacted prompts and argv, raw responses,
  artifact inventories and hashes, timings, warnings, and explicit unknown telemetry.
- [x] Publish a per-application compatibility disposition and gate later full generation on only
  verified requested configurations.

## Fixed controls

- Hosted baseline: `gpt-5.6-terra`, reasoning `medium`, through the actual Codex subscription path.
- Shared calibration only: `gpt-5.6-luna`, reasoning `medium`.
- Local baseline: `qwen/qwen3.8-27b`, reasoning `xhigh`; thinking remains enabled.
- Embedding endpoint where used: `text-embedding-nomic-embed-text-v1.5@f16`, 768 dimensions.
- Operator: Gobby-managed Codex `gpt-5.6-sol`, reasoning `xhigh`, recorded separately from the
  comparator generation model.
- Probe deadline: 900 seconds; at most two hosted generators concurrently; local generation serial.
- One diagnosed whole-run retry is permitted. Failed attempts remain evidence. Unsupported required
  baselines remain visible and are never replaced silently.

## Memory evidence reviewed

Project memory was treated as a guardrail, not authority. The relevant current facts say LM Studio
catalog operations use its native `/api/v1` surface; Gobby must not load, unload, download, or delete
models; and endpoint-backed calls must preserve exact endpoint identity without implicit cloud-key
fallback. The frozen applications and observed runtime behavior below remain the certification
sources.

## Attempt inventory

The shared prompt is
`results/compatibility/hosted-calibration/prompts/calibration-task.txt` in the runtime root. Its
SHA-256 is `11785486e9c1c6af8203ffc63b8865080200cdcc201317354447de6fb2847c22`; both
lanes used byte-identical `source.txt` inputs with SHA-256
`747ba5a8762d142f64d4b1b187eb1066f3206e7deed3e50fe3bc98934e744736`.

| Attempt | Disposition | Result | Wall time | Evidence |
| --- | --- | --- | ---: | --- |
| Managed-worker wrapper preflight | `blocked-preflight` | Shell redirection was denied before Codex launched; zero model calls | 0.014819 s | `results/compatibility/hosted-calibration/preflight-wrapper-failure.json` |
| `gpt-5.6-terra`/medium, attempt 1 | `supported` | Native source read, model-owned file change, post-write read, exact marker, exit 0 | 14.19 s | `logs/compatibility/hosted-calibration/terra/attempt-1/raw-events.jsonl` |
| `gpt-5.6-luna`/medium, attempt 1 | `supported` | Native source read, model-owned file change, post-write read, exact marker, exit 0 | 16.03 s | `logs/compatibility/hosted-calibration/luna/attempt-1/raw-events.jsonl` |

The wrapper denial is orchestration evidence, not a failed Terra attempt. Both actual model probes
passed on their first attempt, so no retry was used. The canonical records are
`results/compatibility/hosted-calibration/run-records.jsonl`; verification is in
`verification.json`, and the detached inventory digest is
`19e8315d6faee0fe8204d1fdc518f8cef8efb4c61afd1999b82aecca6db6ea98`.

## Hosted calibration findings

Both exact hosted configurations are available through Codex 0.153.4 using ChatGPT subscription
authentication while API-key variables are unset. Each invocation used `--ignore-user-config`,
an explicit model slug, and explicit `model_reasoning_effort="medium"`. A filtered live catalog
contains both exact slugs and declares medium reasoning support. Neither raw stream contains a
fallback event, stderr is empty, and there were no warnings or substitutions.

| Model | Input tokens | Cached input | Output tokens | Reasoning output | Native child processes | Model calls |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `gpt-5.6-terra` | 58,733 | 48,128 | 354 | 45 | 2 | `"unknown"` |
| `gpt-5.6-luna` | 52,632 | 46,080 | 472 | 111 | 2 | `"unknown"` |

The JSONL exposes aggregate turn usage but not the number of underlying provider requests, so
`model_calls` is deliberately `"unknown"`, not inferred as one. Native model-call concurrency
and its configured limit were one in each lane. Subscription quota before, after, delta, and unit
were not observable and are also recorded as `"unknown"`.

## Application dispositions

The canonical application-attempt records are
`results/compatibility/application-probes/run-records.jsonl` in the runtime root. They contain 11
records: the seven applications, the failed and diagnosed gcode preflights, the managed-Qwen
preflight and timed run, and OpenDeepWiki's separate catalog and content requests. The detached
package inventory SHA-256 is
`ed8d82be61fe8e6494509f3f008d5907d184d9c16a303d2b661a6c45a1eec44a`. It reports zero fallbacks
and zero provider/model substitutions. Unobservable provider-call counts remain the literal
`"unknown"`.

| Application | Adapter tested | Disposition | Later full-generation gate |
| --- | --- | --- | --- |
| Graphify | Native LLM backend resolution for Codex/Terra | `demonstrated-unsupported` | Semantic extraction/generation blocked; deterministic graph/index lanes eligible |
| gcode | Native embedding status and doctor for Nomic F16 | `blocked-preflight` | Deterministic index/retrieval eligible; semantic embedding lane blocked |
| Understand Anything | Host skill/subagent dispatch | `not-applicable` | Eligible through the verified host Codex Terra/medium profile |
| Archify | Host-authored deterministic renderer/validator | `not-applicable` | Eligible through the verified host Codex Terra/medium profile |
| CodeWiki | Native `codex` CAW backend | `supported` | Eligible with exact Codex Terra/medium configuration |
| OpenDeepWiki | Native OpenAI-compatible catalog and content requests to LM Studio | `supported` | Eligible only while exact Qwen is loaded and its default reasoning remains xhigh |
| Grok Wiki | Native local-CLI Codex adapter | `supported` | Eligible with exact Codex Terra/medium configuration |

### CodeWiki

Frozen CodeWiki `2584854d7538dc3e3e8e6839cf8590b0cd12a431`, CLI 1.0.1, was invoked through its
actual `CawBackend`. The outer command was the isolated Python environment plus
`results/compatibility/hosted-adapters/harness/codewiki_probe.py`; its captured native child argv
was:

```text
exec --sandbox read-only --skip-git-repo-check --json -m gpt-5.6-terra \
  -c model_reasoning_effort="medium" -
```

The model used a native read-only command to inspect the hashed fixture and returned exactly
`CODEWIKI_PROBE_OK:NATIVE_ADAPTER_7F3C91`. The run exited 0 in **11.812850 seconds**. Aggregate
usage was 29,180 input tokens, including 25,088 cached, and 148 output tokens. The result and
trajectory SHA-256 values are respectively
`b5f6e796f43d3a80a34ee3e4b5f24cfd08fdbefa2779691fae73c50de595c57b` and
`17828776cf1fe199e922b5815ecb61ca422b44e48088720149c00769a18841b5`.

CodeWiki's saved configuration has no reasoning field, but the wrapper argv and returned
trajectory both record `medium`. CAW's normalized trajectory leaves the tool output empty because
it reads `aggregated_output`; the raw Codex event retains the successful read. An in-memory
bootstrap-only `tiktoken` stub avoided a blocked eager BPE download, and its `encode_calls` count is
zero, so it did not alter completion behavior. No fallback occurred. `model_calls` is `"unknown"`
because only aggregate turn usage is exposed.

### Grok Wiki

Release 0.0.38 ran the signed bundle's Bun and `rlm-wiki.js` with:

```text
ask <fixture> <prompt> --agent codex --model gpt-5.6-terra \
  --reasoning medium --mode fast --verbose
```

The adapter performed `codex login status` and then one native read-only `codex exec`. The model
read `probe.txt`, returned `NATIVE_ADAPTER_7F3C91`, explained the native read, and included a
`probe.txt:1` source link. The outer process exited 0 in **11.572400 seconds**. Aggregate usage was
30,227 input tokens, including 25,088 cached, and 160 output tokens. The captured built stdin and
raw Codex JSONL SHA-256 values are respectively
`9056b911059666f06c2099d45dee9a5c6b6492d86d2212af3f9910ee365dd71b` and
`f3763f244fe33ea955f77aee63dc099c16a78793c8c9312e5cd191b7a82c4271`.

`GROK_WIKI_SERVER_ENTRY` had to identify the bundled JavaScript because the release's default
points at an absent source-tree TypeScript path. That is a packaging warning, not a provider retry.
The outer Sources parser said none even though the answer contains the inline citation. No fallback
occurred; `model_calls` remains `"unknown"` because Grok exposes aggregate adapter usage, not an
underlying request count.

### Graphify

The frozen `0.9.55` source and executable were both checked. `graphify/llm.py:101-222` enumerates
the available providers, and `graphify/llm.py:1929-1961` rejects an unknown backend before reading
credentials or contacting a model. The isolated no-model resolution command was equivalent to:

```text
extract_files_direct([], backend='codex')
```

It exited 1 in approximately **0.11 seconds** with:

```text
ValueError: Unknown backend 'codex'. Available: ['azure', 'bedrock', 'claude',
'claude-cli', 'deepseek', 'gemini', 'kimi', 'ollama', 'openai']
```

The pinned source, the frozen 0.9.55 executable inventory, and the zero-model-call error jointly
meet the matrix definition of `demonstrated-unsupported`; this is not a missing-result inference.
Graphify's deterministic graph and index lanes may still run, but its required Codex/Terra semantic
generation lane may not be replaced with OpenAI API, Claude, Ollama, or another backend.

### gcode

The pinned gcode 1.7.0 / contract 8 executable was checked against the isolated C0 project. Attempt
0 inherited main-project selectors and failed with a signed-grant project mismatch in about **0.05
seconds**, before an embedding call. The one permitted diagnosed retry removed inherited `GOBBY_*`
selectors except isolated `GOBBY_HOME`; native `status` then verified project
`263a50fd-d414-4dbc-aad4-3c427bf49254`, 142 indexed files, 2,499 symbols, and healthy BM25 indexes.
The decisive command remained:

```text
tools/gcode/bin/gcode embeddings doctor
```

It failed before an embedding request because the scoped runtime database role cannot read
`config_store`. The separately verified cache routes embeddings through the isolated daemon to
`text-embedding-nomic-embed-text-v1.5@f16`, dimension 768, query prefix `search_query: `, but those
configuration facts do not turn the failed doctor into a passing probe. The embedding disposition
is therefore `blocked-preflight`, not unsupported. No embedding or generation model call occurred;
deterministic AST/BM25/graph work remains eligible.

### Understand Anything

Frozen commit `07edf82a04371b6f69779b067bdc8a1a8753a9db` passed its built core-module import. Its activated
project-local skill and agent frontmatter define host subagent dispatch but no application-owned
model or provider fields. The provider-adapter disposition is consequently `not-applicable`, not
an untested success. Later generation is eligible only through the already verified host Codex
Terra/medium profile. The frozen core entrypoint and primary skill SHA-256 prefixes are `92a600ac`
and `1f6acd37`; activation evidence is in `receipts/project-local-skills.json`.

### Archify

Frozen commit `c6519401f7b91b9d43011657880893b0a8955548` is a host-authored deterministic
renderer/validator, not a provider client. Its native check:

```text
node sources/archify/archify/bin/archify.mjs doctor
```

passed every check in **0.03 seconds**. The application provider-adapter disposition is
`not-applicable`; later artifact authoring is eligible through the verified host Codex Terra/medium
profile. The project-local activation is recorded in `receipts/project-local-skills.json`.

### Managed Qwen bootstrap and completion

The first managed attempt failed before spawn because the output-only directory lacked registered
project identity. The diagnosed retry requested and effectively ran exact
`qwen/qwen3.8-27b`/xhigh as agent run `aec76e18-5572-4b8a-b6f6-89bdb1a98f30`. It began at
`2026-09-07T19:15:15.933405Z`, ended at `19:30:31.157236Z`, and timed out after **915.223831
seconds** against the 900-second deadline.

Model bootstrap and tool use passed: six tool calls across six turns included `uuidgen`, and the
model reused the returned UUID. Completion failed because the child lacked a task-scoped file-write
grant. Every attempted artifact write was denied, so there is no persisted source, answer,
post-write verification, `end_agent_run`, or `COMPLETE` marker. No fallback or model substitution
occurred. This is `blocked-preflight`, not evidence that Qwen lacks tool use or is unsupported.

The exact 1,959-byte prompt is
`results/compatibility/application-probes/prompts/qwen-managed.txt`, SHA-256
`62dce5e4ad8cc423fbea0fbf429d31010f5aab50be4d5a9e24ebf2ec073c3814`. The saved capture adds one
terminal newline and hashes to
`14ab8b8984080483a213d20fb1643d015a5b07de802e60e60d5221a215c12a80`; removing only that newline
reproduces the managed-run logical capture hash
`5458009917b234fcc6632fa474075ebcf005e56f4b2b49593bc7bc9e9de70c5f`.

### OpenDeepWiki

OpenDeepWiki commit `75840e5e86213ca40ace9d5036b1f52603f8d038` was configured with its native OpenAI-compatible
provider pointed at `http://host.docker.internal:1234/v1`. Its catalog, content, and translation
models were exact `qwen/qwen3.8-27b`. Provider credentials remain redacted and are not part of the
evidence package.

The native discovery request returned HTTP 200 in **0.029028 seconds**, 6,505 bytes, and found both
exact Qwen and `text-embedding-nomic-embed-text-v1.5@f16`. The native connectivity request returned
HTTP 200 in **0.619883 seconds** and reported 613 ms. LM Studio recorded `GET /v1/models`, followed
by `POST /v1/chat/completions` with exact Qwen, `max_tokens: 1`, `stream: false`, and `ping`. Its
response named exact Qwen and recorded 53 prompt tokens, zero completion/reasoning tokens, and
`finish_reason: length`. The redacted provider-side slice SHA-256 is
`3f7916d1301d2b9ae8f553bf6ae4dc93be357da07b55cfc7156f0863e56f99ec`.

The verified loaded model is 8-bit with context 262,144, parallelism 4, supported reasoning options
off/low/medium/xhigh/on, and default xhigh. OpenDeepWiki's saved model row says
`supportsThinking=false`, and `AdminToolsService.cs:875-893` adds a thinking configuration only for
DeepSeek; the OpenAI request therefore carries no reasoning field. The effective xhigh result comes
from the independently verified LM Studio loaded-model default. This makes the adapter `supported`
only while that default remains xhigh. No fallback occurred. OpenDeepWiki does not use an embedding
adapter in this workflow; discovering an embedding model in the catalog is not an embedding probe.

## Full-generation gate

The compatibility stage is complete, but it does not authorize every lane indiscriminately:

- **Eligible:** CodeWiki and Grok Wiki with their verified native Codex Terra/medium adapters;
  Understand Anything and Archify through the verified host Terra/medium profile; OpenDeepWiki with
  exact Qwen only while LM Studio's loaded-model default remains xhigh.
- **Partially eligible:** Graphify deterministic graph/index work, with semantic generation blocked;
  gcode deterministic AST/BM25/graph retrieval, with embeddings blocked.
- **Blocked:** the managed-Qwen artifact/completion lane. Later work must not count it as a passing
  baseline or silently substitute another provider, model, reasoning level, or execution path.

These gates preserve all required unsupported or blocked baselines visibly while allowing only the
configurations actually demonstrated above to proceed.
