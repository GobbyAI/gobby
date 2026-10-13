# #24215 revision context: OpenAI's Decisions API as a wire

Plan: `.gobby/plans/decision-classifier-path.md`, stamped at 265988bf83.
Josh chose Request changes on 2026-10-12, through the Orchestrator
gobby#15977. Verbatim: "OpenAI has a decisions api we should support as part
of the Jev plan. It's not really a Jev plan but a system one style adapter for
multiple providers."

Required revision (15977):
1. Reverse Decision 1's rejection of `/v1/decisions` and support the OpenAI
   Decisions API as a wire.
2. Reframe the plan as a System One-style adapter across multiple providers
   (Jev, LM Studio `/v1/systemone`, OpenAI decisions, others), not
   Jev-specific. Keep the Graphify leaves' dependency on it.

## Evidence (2026-10-12)

WebFetch of developers.openai.com was EGRESS_BLOCKED for the Writer seat, so
these are WebSearch snippets of the official pages, retrieved 2026-10-12.
Adversary gobby#16491 is asked to read the pages directly.

Official pages:
- Guide: https://developers.openai.com/api/docs/guides/decisions
- Reference: https://developers.openai.com/api/reference/resources/decisions/methods/create
- Model: https://developers.openai.com/api/docs/models/gpt-6-luna
- Data controls: https://developers.openai.com/api/docs/guides/your-data

Endpoint and models:
- `POST /v1/decisions`, public beta. The reference describes it as asking
  classification or scoring questions about the same input.
- Only `gpt-6-luna` is supported. The model page lists one snapshot, the
  undated default `gpt-6-luna`. The reference's example response reports
  `model` as the bare `gpt-6-luna`. No dated snapshot was found.
- The model page lists a 1,050,000-token context window and 128,000 maximum
  output tokens. It documents no maximum input; 922,000 is their difference,
  an inferred default (Adversary gobby#16491, LM-F3, direct read). No
  Decisions-route limit was found.

Request:
- `model`, `input`, and a list of `questions`.
- `input` is a string or an array of `DecisionInputMessage` objects (text
  or images; at most 128 images per request).
- Each question has `type`, `instructions` (the question text), and `name`.
  The reference marks `name` optional; the guide says each question should
  have a unique name, which the response echoes back.
- Types: `predicate` (probability a statement about the input is true),
  `choice` (one value from supplied options), and `score` (ordered levels;
  each level has a label and an optional description).
- A Choice needs 2 to 255 unique `choices`, each with `value` and
  `description`. A string and a boolean with the same text are distinct
  values.
- Maximum question count: not found in the official snippets. Third-party
  sources disagree (one says 1 to 200, one says no cap), so none is cited.
  The plan's 64 is below every reported figure.

Response:
- A Decision object with `answers`, `model`, and `usage`.
- `answers` is an array matched by `name`:
  - predicate: `probability`;
  - choice: `choice`, `confidence`, and `probabilities`, an array of
    `{value, probability}`;
  - score: `score`, `confidence`, and `probabilities` entries with `label`,
    `probability`, and value;
  - refusal: `{name, type: "refusal"}`. The model declined that question,
    and other questions in the same request still get answers.
- `usage` carries `input_tokens`, `output_tokens`, and `total_tokens`. The
  docs' example, one predicate on a 42-token input, reported
  `input_tokens: 42` and zero output tokens, which suggests `input_tokens`
  counts the `input` field. Its scope is unverified.

Data and price:
- The data-controls page lists `/v1/decisions` as not used for training,
  with abuse-monitoring retention of up to 30 days and no application state
  retention, except that prompt caching may keep encrypted KV tensors on
  OpenAI's GPU machines for up to 24 hours (LM-F5). Gobby sends text only,
  so the image exception does not apply. The guide says the API supports
  Zero Data Retention and HIPAA use for eligible customers; ZDR needs
  OpenAI's prior approval. No request-level privacy flag was found.
- Price: the Decisions guide gives a base rate of $0.10 per million input
  tokens for `gpt-6-luna`, with no cache or output charges. The model page
  doubles the input rate above 272K input tokens, and regional processing
  adds 10% (LM-F4, direct read by gobby#16491). The community announcement
  (https://community.openai.com/t/decisions-api-is-now-available-in-public-beta/1403877)
  states the same base rate.

Unverified: error statuses and bodies on this route, rate limits, the
Decisions-route input limit and truncation behavior, the scope of
`usage.input_tokens`, the question `name` pattern, the maximum question
count, and whether a response ever reports a resolved version.

LM Studio: its `POST /v1/decisions` is documented as "OpenAI compatible".
The 2026-10-12 probes (`decision-classifier-lmstudio-24215.md`) showed it
requires `model`, `input`, and a `questions` array, and that a Choice item
needs `instructions` and `choices`: the OpenAI shape. No new probe was sent.

## Direction

- Contract: Gobby's System One-style decision, a state plus typed questions
  in and calibrated typed answers per question key out. A wire adapts it to
  one provider shape. Keyed shape: `systemone`, `openrouter-decisions`,
  `workers-ai`. Array shape: new `openai-decisions`, posting to
  `{api_base}/v1/decisions`, which also reaches LM Studio's route.
- Mapping: `input` is the state as compact JSON text in the caller's key
  order; each question carries `name` (the Gobby key), `type`, and
  `instructions`; a Choice sends `choices` as `{value: option key,
  description: option text}` in criteria order; Noul is sent as `predicate`.
- Refusal: `DecisionsUnavailable(reason="refused")` for the whole call, a
  local outcome that never opens the cooldown.
- `backend_max_state_tokens` default for the wire: 922,000, inferred from
  the model page's context window minus its maximum output, with the
  input limit, its scope and truncation unverified.
- Identity: `model_card` does not apply; under `response_version` the
  documented response echoes the undated alias, so the alias check and the
  version pin fail. OpenAI is a `shadow` and evaluation backend until OpenAI
  publishes a pinnable dated snapshot, as Josh's Decision 16 option A set
  for hosted Clef. Josh may revisit that; the plan adds no contract.
- Extension point for other providers: the `wire_api` value plus one
  request builder and one response reader per shape. No provider registry.
- Rename the not-yet-created `docs/evidence/decisions/systemone-wire.md` to
  `docs/evidence/decisions/wire-contracts.md`.
- The Graphify parity plan references only #22604, whose dependency on 1.2
  is unchanged.

## #24253: defer `openai-decisions` until OpenAI Decisions API GA

Josh, button 381243f3 (value `openai-decisions:defer-ga`, 2026-10-12),
supersedes identity button d8eab611. Orchestrator gobby#15977 gave go on
2026-10-13. The other wires proceed unchanged: `systemone` (LM Studio,
Kev), OpenRouter (Jev), and Workers AI (Clef).

- LM Studio: its example and Decision 17 already use `/v1/systemone`, and
  `/v1/decisions` was only reachable through this wire. No non-OpenAI
  backend depends on it, so the wire and LM Studio's `/v1/decisions` defer
  together. Josh's proceeding list is unchanged.
- `refused` exists only for OpenAI refusal answers, so it defers too.
- GA re-checks: (1) whether dated snapshots or a resolved `model` in the
  response now exist (identity contract, Decision 10); (2) the Vercel AI
  Gateway `gpt-6-luna-decisions` listing as a possible host. Source: the
  task text and button; no Vercel URL retrieved.
- New `## D1` after 4.1, before V2, in the shape of hub-mode's D2. It
  depends on 1.1, 1.2, 2.1, 3.1 and 4.1. Its yaml carries a placeholder
  task_ref and owner orchestrator. Its Original acceptance items D1.1 and up
  carry the old 1.2.16, 1.2.17, 4.1.6, the OpenAI parts of 1.1.1, 1.1.4,
  1.1.10, 1.1.15, 2.1.7 and 3.1.1, and LM Studio's `/v1/decisions` facts.
- Orchestrator duties: file the D1 task under #24238 with labels
  `deferred-from:decision-classifier-path:D1` and `needs-planning`,
  criteria repeating each D1.x ID, and blocked-by the leaves for the
  sections D1 depends on. Also edit the leaves: 1.1 #24243, 1.2 #24244,
  2.1 #24245, 3.1 #24246 and 4.1 #24249 (1.2.16, 1.2.17 and 4.1.6 removed).
- Coverage manifest (plan_hash af1af00c) regenerates after M1 and the
  leaves settle, under #24224.

Progress: every narrative edit is done, D1 holds D1.1 to D1.7, the V1
entry is written, and the M1 block is withdrawn. The final sweep is clean.
Remaining: validate (standard and expansion) after the #24232 cutover's
DAEMON BACK; ocr per path; commit the plan under #24253 and this note under
#24215 (15977's request); SHA to 16491 and 15977. Prior M1 routing to
re-send: 1.1 to 3.3 code, feature, backend, tdd true; 4.1 docs, chore,
tdd false, tech-writer.
