# Decision wire contracts

Pinned contract record for the `DecisionService` wires in `src/gobby/ai/decisions.py`
(plan `decision-classifier-path`, task #24244, acceptance items 1.2.6, 1.2.14 and 1.2.15).
The parser and the fixtures under `tests/ai/fixtures/` use the field names below.

## Provenance and limits of this record

- This record transcribes the facts that plan Decisions 1, 8, 9, 10, 15 and 17 cite, with
  the retrieval dates the plan records. The plan is the primary record for those dates.
- This seat (2026-10-23) had no egress. The network proxy blocked
  `docs.typesafe.ai`, `openrouter.ai`, `developers.cloudflare.com`, `lmstudio.ai` and
  `github.com`. Nothing below was re-fetched in this session.
- The Kev source at commit `0fe8fc97c2bc` could not be read here. Its `/v1/models` card
  key names are not recorded anywhere this seat could reach.
- Fixtures prove conformance to the documented shapes only. They make no claim that any
  server is compatible at runtime. The Activation Gate's live capture establishes that.

## 1. TypeSafe `systemone` (Kev reference wire)

- URL: `{api_base}/v1/systemone`. The default wire, `wire_api: systemone`.
- Request body: `model`, `state`, and `questions`. `questions` is a map keyed by question
  ID. Gobby policy requires each Choice question to carry `type: "choice"`, non-empty
  string `instructions`, and `criteria`, a map from option key to string-only text.
- Response body: `model` (a non-empty string; TypeSafe resolves `jev-latest` to a version
  such as `jev-1.13.0`), `answers` keyed by question ID, and `usage.input_tokens`.
  Each answer carries `type` (equal to its question's type), `choice`, `probabilities`
  (keyed by option), and `confidence`. TypeSafe requires `probabilities` and `confidence`
  on a Choice answer.
- Gobby request-profile limits: 2 to 255 Choice options; 1 to 64 questions; question keys match
  `^[A-Za-z0-9_.-]{1,100}$`. Documented whole-request limit 64k tokens; 32k for state
  plus the longest question (`https://docs.typesafe.ai/models`, per Decision 8).
- Noul (`type: "noul"`, a yes/no proposition): Gobby's question is exactly
  `{"type": "noul", "instructions": <non-empty string>}`, with no other keys. The answer
  carries `type: "noul"` and the probability in its `noul` field, finite and in [0,1]. A Noul
  answer carries no separate `confidence` (`docs/research/jev.md` lines 35-36). Recorded from
  plan Decision 1 (`.gobby/plans/decision-classifier-path.md` lines 1421, 1829-1830 and
  2100-2102). Not re-fetched this seat.
- Source: `https://docs.typesafe.ai/api.md` and `https://docs.typesafe.ai/models.md`,
  retrieved 2026-09-29 per Decision 1. Not re-fetched this seat.

## 2. Kev reference facts (commit `0fe8fc97c2bc`, 2026-09-29)

Recorded from plan Decisions 2, 8 and 10. Not re-read from source this seat.

- `MODEL_NAMES` accepts `kev-latest` and `jev-latest` for any loaded checkpoint, and
  `response.model` echoes the requested name. The alias alone does not identify the
  checkpoint.
- `SERVE_MAX_STATE` is 65,536 state tokens. `SERVE_MAX_BRANCH` is 73,728 branch tokens.
  Training uses 384 state tokens.
- `Server.submit` calls `encode` without `strict=True`. An overlong state is truncated
  from its end with no error.
- `usage.input_tokens` is the encoded length after truncation (`Server._body`).
- `/v1/models` card fields: unverified. Decision 10 names the stable fields as run path,
  base model, LoRA config, dtype and calibration temperature. The key names below are
  the parser's assumption, not a transcription.

  Identity keys used by the parser: `run_path`, `base_model`, `lora_config`, `dtype`,
  `temperature`. A card entry that lacks any of them yields no identity. Unverified
  until the live capture reads the real card.
- Search lead, unread: a web search named `github.com/jaredpalmer/kev` as the Kev project.
  The live capture should check the repo against commit `0fe8fc97c2bc` before the card
  keys are trusted.

## 3. OpenRouter decisions (`openrouter-decisions`)

- URL: `{api_base}/alpha/decisions`, with `api_base` `https://openrouter.ai/api`.
- Auth: `Authorization: Bearer <api_key>`.
- Request body: `model`, `state`, `questions`, and `provider: {"zdr": true,
  "data_collection": "deny"}` on every request. The `provider` field is set by the wire
  and has no config knob (Decision 1).
- Response body: `id` (optional), `model` (a dated snapshot such as
  `typesafe/jev-1.13-20260917`), `answers`, optional `provider`, and `usage` with
  `input_tokens`, `output_tokens` and `cost`. The parser ignores `id`, `provider`,
  `usage.output_tokens` and `usage.cost`.
- Choice answers: OpenRouter's reference marks `probabilities` and `confidence`
  optional. The parser requires them, the stricter reading, so a Choice answer without them
  raises `parse`.
- Documented input limit: 32,000 tokens for the whole input, state plus questions
  (`https://openrouter.ai/docs/guides/community/jev.md`, retrieved 2026-09-30 per
  Decision 8).
- Documented error statuses: 400, 401, 402, 403, 404, 413, 429, 500, 502, 503, 524, 529.
  Error body: `{"error": {"code", "message"}}`.
- Source: `https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request.md`,
  retrieved 2026-09-30 per Decision 1. Not re-fetched this seat.
- Unverified: oversize behavior (a 413 is documented, its truncation is not), decision-specific
  rate limits, and whether a dated slug is accepted as a request model.

## 4. Cloudflare Workers AI Clef (`workers-ai`)

- URL: `{api_base}/ai/run/@cf/cloudflare/{model}`. `api_base` is
  `https://api.cloudflare.com/client/v4/accounts/<account_id>`. `model` is `clef` or
  `clef-flash`.
- Auth: a bearer API token with Workers AI Read and Edit.
- Request body: `model`, `state`, and `questions`, with no `provider` field.
- Response: the REST envelope `{result, success, errors, messages}`. The parser reads
  `result`, which carries `model`, `answers`, and `usage` with `input_tokens` and
  `output_tokens`. A 2xx body with `success` false, or without an object `result`, raises
  `parse`. The parser ignores `errors` and `messages`.
- Input limits: 1 to 64 questions; keys match `^[A-Za-z0-9_.-]{1,100}$`; `instructions` on
  every question; 2 to 255 Choice options.
- Context window: 65,536 tokens. Long text state is truncated to fit.
- Prices: $0.24 per million input tokens for Clef, $0.09 for Clef-flash, no output price.
- Sources: `https://developers.cloudflare.com/workers-ai/models/clef/`,
  `.../models/clef-flash/`, and `.../get-started/rest-api/`, retrieved 2026-10-08 per
  Decision 15. Not re-fetched this seat.
- Unverified: error statuses and bodies on this route, rate limits, the scope of
  `usage.input_tokens`, the truncation point, and versioning. The live capture settles
  them.

### Local Clef (`systemone` wire)

Read from the `clef_mlx.py` copy in `mlx-community/clef-flash-8bit`, retrieved 2026-10-08
per Decision 15. Not re-read this seat.

- `clef_mlx.py serve` binds `127.0.0.1:8000` by default. It serves `POST /v1/systemone`,
  `GET /v1/models`, and `GET /health`.
- Default limit: 16,384 tokens for the whole prompt. `usage.input_tokens` counts the whole
  prompt. The state is silently truncated by default. With `--no-truncate` the server
  answers 413. A request's own `truncate` field overrides the default, and Gobby never
  sends it.
- `model` is echoed from the request.
- `GET /v1/models` entries carry only `id` and `object`. Under `model_card` such a card
  yields no identity.
- The server has no authentication and fetches remote image URLs a request names. Gobby
  sends text state only. Keep it bound to `127.0.0.1`.
- `clef_mlx.py` runs one request at a time under a lock.
- Operator sets `backend_max_state_tokens: 16384`.
- Cloudflare's release (`https://huggingface.co/Cloudflare/clef` and `Cloudflare/clef-flash`)
  ships BF16 weights and `joint_schema_model.py`. Its `systemone` is a Python function with no
  HTTP server. It defaults to CUDA and was tested on one H200. The mlx-community conversions
  above ship the HTTP server this section covers. Recorded from plan lines 490-494 and
  1292-1293. Not re-read this seat.

## 5. LM Studio `/v1/systemone`

- Minimum: LM Studio 0.4.26 Build 4. The 0.4.26 changelog is dated 2026-10-08, and it
  lists the `/v1/systemone` route under Build 4, after Builds 1 to 3. Bionic 1.1.8 requires
  0.4.26 or newer.
- URL: `{api_base}/v1/systemone`. `api_base` is the server root, such as
  `http://127.0.0.1:1234`.
- LM Studio's docs say `/v1/systemone` takes `state` and `questions` keyed by ID, supports
  noul, choice and score, returns `answers` keyed by question ID, and takes a loaded
  decision model's identifier. Source: LM Studio docs and changelogs, read as WebSearch
  snippets and the Plan Adversary's direct reads on 2026-10-12 (plan gobby#16491).
- Upstream reference only, not runtime evidence: llama.cpp `POST /v1/systemone`
  documentation, which describes `usage.input_tokens` as prompt tokens across all
  questions. LM Studio's load docs expose a `context_length` field.
- Observed on the operator's server at `127.0.0.1:1234` on 2026-10-12 (research note
  `.gobby/plans/research/decision-classifier-lmstudio-24215.md`):
  - `/v1/systemone` requires `state` and `questions`. It raises no error for a missing
    `model`. It requires `instructions` on a Noul and `criteria` on a Choice.
  - A complete Noul with no `model` field, sent while only an embedding model was loaded,
    answered `noModelsLoaded`. The route did not treat the embedding instance as a loaded
    model.
  - `GET /v1/models` cards carry only `id`, `object`, and `owned_by`. The card has none of
    the Decision 10 identity fields, so `model_card` yields no identity.
  - Both model lists answered without a token. LM Studio can require one; the bearer then
    comes from `api_key`.
- Unverified, since no decision model was installed: the response shape and answer types,
  whether `usage.input_tokens` is present and what it counts, whether `response.model`
  echoes the request or resolves a version, the input limit and truncation, whether
  `/v1/systemone` honors the bearer token, just-in-time loading on this route, and which
  decision models LM Studio serves.
- `/v1/decisions` speaks the deferred `openai-decisions` shape (Decision 18, D1). OpenAI
  declared the Decisions API GA on 2026-10-22, but the wire stays in D1 until OpenAI publishes
  a dated `gpt-6-luna` snapshot or a response `model` identifies the served snapshot. The LM
  Studio `/v1/decisions` facts therefore remain in D1, and this record does not pin them.
  Acceptance item 1.2.15 covers `/v1/systemone` only.

## 6. Unverified items, summary

| Wire | Unverified |
| --- | --- |
| `systemone` (Kev) | `/v1/models` card key names; live `usage.input_tokens` semantics against the card |
| `systemone` (TypeSafe) | Live `usage.input_tokens`; the documented `model` resolution |
| `openrouter-decisions` | Oversize behavior; decision rate limits; dated slug acceptance |
| `workers-ai` | Error statuses and bodies; rate limits; `usage.input_tokens` scope; truncation point; versioning |
| LM Studio `systemone` | Response shape; `usage.input_tokens`; `model` echo; input limit; bearer; just-in-time loading |

Every unverified item keeps a consumer in `shadow` until the Activation Gate's live capture
settles it.

## 7. Gobby policy and Noul fixtures (2026-10-24)

The required instructions, string-only Choice descriptions, two-option minimum and Noul
shape above are **Gobby policy**, a narrower request profile than current upstream.
TypeSafe OpenAPI 0.2.0 allows optional instructions, 1 to 255 Choice options with string,
object, array or null descriptions, and optional Noul `criteria.true` and `criteria.false`.
OpenRouter requires instructions; when Noul criteria are supplied it requires both keys.
Gobby sends exactly `type` and non-empty string `instructions` for each Noul proposition.

The three `*_noul_response.json` fixtures cover keyed probabilities on systemone,
OpenRouter (dated model, id, provider and cost), and the Workers AI REST envelope.
The parser requires `type: noul` and a finite `noul` value in [0,1], with no separate
confidence. Missing keys fail closed. Noul shares Choice's request ceiling, cooldown,
retry, identity, truncation checks and redacted call log. The public request estimate
serializes the same body used by the oversize guard.

Sources retrieved by researcher gobby#17515 on 2026-10-24, transcribed in
`.gobby/plans/research/community-label-decisions-22604.md`:
[TypeSafe OpenAPI](https://api.typesafe.ai/openapi.json),
[TypeSafe system-one reference](https://docs.typesafe.ai/api/system-one),
[OpenRouter OpenAPI](https://openrouter.ai/openapi.json), and
[Cloudflare Clef schema](https://github.com/cloudflare/cloudflare-docs/blob/production/src/content/workers-ai-models/clef.json).
These are documented-schema fixtures; runtime support still requires a live capture.
