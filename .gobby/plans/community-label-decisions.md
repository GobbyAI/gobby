Plan artifact: `.gobby/plans/community-label-decisions.md`

# Community-label decisions across every decision shape

**Plan ID:** community-label-decisions

## Context
`kind: framing`

#22604, **Gate generated names through a Jev `choice` decision**, is the first product
consumer of `DecisionService` (`src/gobby/ai/decisions.py`, landed by #24244). Its
description has three layers: the stamped 6.3 text of
`.gobby/plans/completed/gcode-import-communities.md`, the "Consumes the
decision-classifier-path plan" section that overrides 6.3, and Josh's 2026-10-24 widening:
"So we can test noul, multiple choice, etc." Josh then ruled J3 = B on 2026-10-24: "B, we
need full compatibility with the decision spec." On the same day he ruled D6 = B, adding
Box, the fourth primitive in current TypeSafe and OpenRouter schemas. This plan is the
execution spec for all of it.

The plan delivers four question shapes on `DecisionService` and a real consumer for each:

- **Choice** gates generated community names (6.3).
- **Noul** checks whether a stale model name still fits a community whose membership
  changed, and keeps it without regenerating when it does.
- **Score** rates a community's deterministic name before generation, and skips generation
  when that name is already clear.
- **Box** locates a described target in an image. Its consumer is the agent-facing
  `gobby-decisions:evaluate` tool (#24248), per D7a = A.

Choice, Noul and Score each get one live-path test here: a real `DecisionService` against
a stubbed wire at the `httpx` boundary, asserting the decision drives product behavior.
Box's live-path test belongs to #24248 with its tool. 1.3 proves the Box wire with
fixtures. Live evidence follows:
operator-run captures per wire for the decision-classifier-path Activation Gate (item 4),
then the Q1.6 label-quality study, which sets the ship configuration.

Expansion root: #22604 becomes this plan's epic and keeps its six
`covers:gcode-import-communities:6.3:*` labels. Anchor map, in prose, since the completed
plan's M1 is not edited:

- 6.3.1 is already substituted by #24244's 1.2.1 test. 2.1 adds the batch sizing to
  `service.max_input_tokens` that the substituted 6.3.1 text names.
- 6.3.2 maps to 2.1's `test_gate_admits_at_threshold_and_rejects_below`.
- 6.3.3 maps to 2.4.
- 6.3.4 maps to 2.1's `test_gate_outcomes_extend_the_discriminator_to_nine`.
- 6.3.5 maps to 3.4.
- 6.3.6 is substituted by the Activation Gate captures, which 3.2 delivers.

Working note with the repository facts, rulings and transcribed wire schemas:
`.gobby/plans/research/community-label-decisions-22604.md`.

## Decision Record
`kind: framing`

1. **D1 = A (Orchestrator, 2026-10-24): every decision lands in Python now.** #24701,
   **Symbol summaries and community labels** (code-index-family 8.3), currently deletes
   `community_labeler.py`, `community_label_safety.py` and `_storage/communities.py`, and
   writes `label_confidence` NULL. It must preserve the Choice gate, the Noul carry check
   and the Score sufficiency check in its native job, through a hub decisions route or a
   Rust client. It must not delete them silently. The Orchestrator amends 8.3 before #24701
   dispatches, and adds a `blocked-by` edge from #24701 to #22604. #24701 then waits until
   every leaf here closes, the Python study included, because 8.3 deletes the labeler,
   `StoredCommunity` and `_start_code_index_tasks` that this plan builds on (Coordination).
2. **D2 = A (Orchestrator): this plan builds `DecisionService.noul`.** That covers its
   three fixtures, its wire note, and
   `tests/ai/test_decisions_service.py::test_noul_returns_probabilities_by_key`. At
   expansion those leave #24246, **MCP tool reranking through Noul** (Coordination).
3. **J1 = A (Josh): backends.** Jev is reached over OpenRouter
   (`wire_api: openrouter-decisions`, `identity_contract: response_version`). Its key is
   already in Gobby secrets. Agents cannot read the secrets table, so every keyed command
   is operator-run: Josh runs the prepared commands.
   - LM Studio at `127.0.0.1:1234` (`wire_api: systemone`, version 0.4.26 Build 4 or newer)
     is the shadow and evaluation backend (decision-classifier-path Decision 17). Its model
     id is Josh's to fill in. The default is `ggml-org/Clef-Flash-GGUF`
     `Clef-Flash-Q8_0.gguf` (9B, about 22.7 GB RSS); the comparator is `Clef-Q8_0` (27B,
     about 42 GB). MLX conversions lack the decision head.
   - The Kev and direct TypeSafe options are removed. Runtime support for 64-question
     batches, Noul, Score and Box on LM Studio stays unverified until its capture.
4. **J2 = A (Josh): Josh rates.** He rates every blinded item: about 90 D/G/J names plus
   30 carry items. The study writes the sheet as a file that the Assistant sends him, and
   its `score` step reads his completed sheet back.
5. **J3 = B and D3 = A: Score is built, and its consumer is deterministic-label
   sufficiency.** Before generating, the labeler Scores the community's deterministic
   label on four ordered levels: misleading, vague, clear, clear and specific. In enforce,
   a position at or above `min_score` writes the deterministic label and skips generation.
   This supersedes decision-classifier-path Decision 7 ("Score has no consumer and is not
   built") (Coordination). The evidence reuses the D and G ratings, so Josh rates nothing
   extra. Rejected alternatives:
   - B, a memory-relevance consumer, is planned elsewhere and would couple this plan to it.
   - C, a periodic re-score sweep, adds a loop with no current need.
6. **Score wire profile (Orchestrator): strict and fail-closed.**
   - Request: `{"type": "score", "instructions": <non-empty string>, "criteria":
     [<level>, ...]}`, with 2 to 10 levels, lowest first.
   - Answer: all five fields are required: `type`, `score`, `confidence`, `legend` and
     `probabilities`.
   - `legend` and `probabilities` carry exactly the keys `"0"` to `"n-1"`, in request
     order. Each probability and the confidence is finite and in [0,1]. The probabilities
     sum to 1 within `_SUM_TOLERANCE`.
   - `score` lies in [0, n-1] and equals the probability-weighted index within
     (n-1) x `_SUM_TOLERANCE`.
   - Any violation raises `DecisionsUnavailable("parse")`. The service never fills a gap.
     OpenRouter's schema makes `legend`, `probabilities` and `confidence` optional; an
     answer that omits them fails this profile, and the capture records that as a finding.
   - `ScoreAnswer` keeps `score`, `probabilities` and `confidence`. It drops `legend` after
     validating it, because the consumer already knows its own levels.
   - Consumers compare the position `score / (n - 1)` in [0,1], so `min_score` sits beside
     `min_confidence` and `min_probability` as a unit-interval float with the same 0.5
     default.
7. **D4 = A (Orchestrator): Game Goblins C1 data source.** The corpus at
   `/Users/josh/Projects/wiki-bakeoff-code-2026-09/corpora/gcode/C1` already carries
   `.gobby/project.json`, with id `c8e071af-e1f7-434f-b3c0-f31a1b1299c9`, name
   `gcode-bakeoff-C1`, created 2026-09-19 (`thresholds.md` precedent). On 2026-10-24,
   `gobby projects list` showed only `gobby`.
   - At study time, and only after Josh's explicit go, the Orchestrator re-registers and
     re-indexes that existing project id. It is never a fresh init.
   - The study reads its rows read-only and writes no live labels.
   - After 3.4 lands, the Orchestrator soft-deletes the registration with
     `gobby projects delete`. The folder and its marker are never removed.
   - Rejected: a fresh isolated database. `gobby_gcode_test` no longer exists, and
     `gobby_test` has no `code_communities` table.
8. **D5 = A (Orchestrator): the ship-rule gap.** Suppose G regresses more than 5% below D
   and J fails any bar. Then the release ships D: `code_index.community_label.enabled:
   false`, with every decision consumer `off`.
9. **D6 = B, D7a = A and D7b = A (Josh, 2026-10-24): Box is built here, and evaluate
   consumes it.** Box asks where a described target is in an image, and answers with one
   rectangle and a confidence.
   - 1.3 builds `DecisionService.box`: a strict parser, fixtures, and two explicit,
     separate transports. OpenRouter carries an inline `image_url` data-URL part.
     TypeSafe `/v1/systemone` carries an `assets` entry with the danger-zone header.
   - Community labels carry no images. Box's consumer is the agent-facing
     `gobby-decisions:evaluate` tool (#24248), which takes a local PNG or JPEG path. #24248
     owns the tool and Box's live-path test (Coordination).
   - The Box backend is OpenRouter with `typesafe/jev-1.14`, on Josh's existing key. If
     the capture shows OpenRouter cannot serve Box, Box stays unverified and evaluate
     returns the typed `DecisionsUnavailable` reason.
   - Rejected consumers:
     - a daemon import of gobby-annotate captures needs its own ingest plan in Josh's
       Annotate subtree (#22225);
     - building Box with no consumer breaks J3's real-consumer rule.
   - Rejected backend: a TypeSafe key adds a credential that the OpenRouter route makes
     unnecessary.
10. **One pinned Jev for the study and the captures.** Box requires Jev 1.14, and
    OpenRouter reports `typesafe/jev-1.14-20261014` serving Box. The captures and the study
    therefore pin `typesafe/jev-1.14`, so a single `evaluated_model` and a single captured
    `backend_identity` cover every shape. The study's J arm runs on this OpenRouter
    backend because only `response_version` there yields a `backend_identity` that enforce
    accepts. LM Studio's `/v1/models` card carries no identity fields, so it can only
    shadow.
11. **Effective enforce (decision-classifier-path Decision 10).** A consumer acts in
    enforce only when all three hold:
    - its `mode` is `enforce`;
    - `evaluated_model == config.model`;
    - `evaluated_backend == result.backend_identity`.
    Otherwise it runs as shadow and logs the warning `evaluated_backend_mismatch` once per
    `failure_cooldown_seconds`. Consumers read `mode` and their thresholds from the resolver
    on every call, so a policy reload takes effect on the next batch.
12. **Thresholds stay at their defaults and are not tuned.** `min_confidence`,
    `min_probability` and `min_score` all default to 0.5. The study measures each consumer
    at its default. A consumer that fails its bars ships `off`. Tuning a threshold would
    need a new study, because the same ratings cannot both choose a threshold and validate
    it.
13. **The carry and sufficiency checks keep the nine-value discriminator.** 6.3.4 fixes
    the outcome event at exactly nine values, so each new check emits its own event:
    `code_index.community_label.carry` and `code_index.community_label.sufficiency`. A
    community that one of them settles never reaches generation, so it emits no outcome
    event. The batch info line gains `carried` and `sufficient` counts.
14. **Rejected alternatives, carried from 6.3 and extended.**
    - A general decision-provider abstraction: `DecisionService` already is the one
      boundary.
    - Routing the gate through `call_json_feature`: a text model's self-reported confidence
      is uncalibrated, and calibration is the reason for the gate.
    - Asking Jev to name a community: it selects among fixed keys and cannot emit text.
    - LM Studio as the study backend: it has no identity, so it cannot enforce.
    - Splitting packed requests by retrying on `oversize`: estimating before sending costs
      one public method and no retry loop.

## Coordination
`kind: framing`

Orchestrator edits named by these rulings. This plan's leaves never edit another plan's
file or task.

Orchestrator task edits, applied verbatim at expansion after Josh approves:

1. #24246, field `dependencies`: add `depends_on` this plan's 1.1 leaf. Fields
   `description` and `validation_criteria`: remove `noul`, its three Noul fixtures, the
   wire-contracts Noul note and
   `tests/ai/test_decisions_service.py::test_noul_returns_probabilities_by_key`. Replace
   the 3.1.1 anchor with "satisfied by community-label-decisions 1.1.1".
2. #24248, field `dependencies`: add `depends_on` this plan's 1.3 leaf. Field
   `validation_criteria`: replace `test_evaluate_choice_and_noul_round_trip` with
   `test_evaluate_round_trips_every_decision_shape`, and append: "Box:
   `gobby-decisions:evaluate` accepts an absolute PNG or JPEG path, calls
   `DecisionService.box`, and returns `{"box": {"top", "bottom", "left", "right"},
   "confidence"}` by key, or the typed `DecisionsUnavailable` reason when the backend
   cannot serve Box. Score returns `{"score", "probabilities", "confidence"}`. test:
   `tests/mcp_proxy/tools/test_decisions_tools.py::test_evaluate_round_trips_every_decision_shape`."
3. #24701, field `dependencies`: add `depends_on` #22604. Field `description`: append
   "Preserve #22604's Choice gate, Noul carry and Score sufficiency decisions, with their
   tests, in the native job (D1 = A). Never delete them silently. Start only after #22604
   closes, its Python study included." The code-index-family 8.3 text gets the same
   amendment.

This plan's 1.3 closes on the parser, fixtures and wire note. #24248's close is the
completion gate for Box's consumer, and 3.5 records it.

- **#24701 / code-index-family 8.3 (D1).** Preserve the gate, carry and sufficiency
  decisions, with their tests, in the native job, and stop deleting them. The Orchestrator
  amends 8.3 before #24701 dispatches, and adds a `blocked-by` edge from #24701 to #22604.
  That edge serializes the native replacement after this plan's Python work and study.
  8.3 deletes `community_labeler.py`, `test_community_labeler.py`, `StoredCommunity` and
  `_start_code_index_tasks` (`.gobby/plans/code-index-family.md`, lines 4300 to 4335).
- **#24246 / decision-classifier-path 3.1 (D2).** At expansion, remove `noul`, its three
  fixtures, the wire note and the 3.1.1 test from #24246. Substitute its 3.1.1 anchor with
  this plan's 1.1.1, and add a `blocked-by` edge from #24246 to this plan's 1.1 leaf. This
  replaces decision-classifier-path's statement that no ordering edge ties #22604 to 3.1.
- **decision-classifier-path Decisions 6 and 7 and #24248 (J3, D6).**
  - Decision 7 is superseded: Score and Box are built here.
  - Decision 6 and #24248, **Agent-facing `gobby-decisions:evaluate` MCP tool**, expose
    all four shapes: Choice, Noul, Score and Box.
  - A Score answer is `{"score", "probabilities", "confidence"}`. A Box answer is
    `{"box": {"top", "bottom", "left", "right"}, "confidence"}`. A Box input takes an
    absolute image path through `gobby.llm.image_payloads.prepare_image_data`, PNG or JPEG
    only.
  - #24248's test `test_evaluate_choice_and_noul_round_trip` becomes
    `test_evaluate_round_trips_every_decision_shape`.
  - #24248 owns Box's live-path test (D7a = A). At expansion, #24248 gets a `blocked-by`
    edge on this plan's 1.3 leaf, and its validation criteria gain the Box round trip.
    This plan's 3.5 waits on #24248 and records that test as evidence.
- **decision-classifier-path Activation Gate item 4.**
  - Evidence paths become `docs/evidence/decisions/<wire_api>-live.md`, with one dated
    section per capture. The previous form was `<wire_api>-live-<date>.md`.
  - Item 4 adds a Score round trip, and a Box attempt per wire.
  - #22604's substituted 6.3.6 text ("under docs/evidence/decisions/") already fits.
- **#24249, Decision capability guide rows.** It edits `docs/guides/llm-features.md`
  later, in different rows from 2.4's.
- **gcode-import-communities.** Its M1 stays untouched; the anchor map is in Context.
- **D4 registration and cleanup.**
  - Before 3.4's study run, the Orchestrator asks Josh for go, then re-registers and
    re-indexes project `c8e071af-e1f7-434f-b3c0-f31a1b1299c9`.
  - Before handing over, it confirms with a read-only count that C1 has at least 15
    communities with three or more members.
  - After 3.4 lands, it soft-deletes the registration. The study ignores stored `label` and
    `label_source`, so background labeling of C1 by the daemon cannot affect it.
- **Restart.** After P2 lands, the Orchestrator runs `uv run gobby restart`, announced with
  a `global` `send_message` and run in a quiet window.

## Constraints
`kind: framing`

- Run every pytest with `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
  GOBBY_TEST_PROTECT=1`, and target only the files each leaf names. Never run the full
  suite.
- Agents never read secrets or credential stores. The capture and study commands resolve
  keys through `src/gobby/storage/secrets.py::SecretStore.get` under the operator's
  database role, so Josh runs them. A developer requests the run through Triage, and
  the Assistant relays it to Josh.
- Neither script reads or writes the live `ai.decisions` config. Each builds its own
  `DecisionsConfig` from flags. Changing the live config is Josh's action after 3.4.
- Monolith ceiling: `decisions.py` is 426 lines and `community_labeler.py` 219. Both stay
  under the 850-line decomposition heuristic after this plan.
- Wire facts come from `docs/evidence/decisions/wire-contracts.md` and the working note,
  never from memory.

## P1: Decision shapes
`kind: framing`

### 1.1 `DecisionService.noul` and the request estimate [category: code]
`kind: deliverable`

Targets:
- `src/gobby/ai/decisions.py::*` — scope-reason: add NoulAnswer, DecisionService.noul and DecisionService.estimate_request_tokens, and generalize the request check, body, parse, dial and schema-hash helpers over question type
- `tests/ai/test_decisions_service.py::*` — scope-reason: add the Noul wire, malformed-answer and request-estimate tests
- `tests/ai/fixtures/systemone_noul_response.json`
- `tests/ai/fixtures/openrouter_decisions_noul_response.json`
- `tests/ai/fixtures/workers_ai_noul_response.json`
- `docs/evidence/decisions/wire-contracts.md`

Add `async noul(consumer, state, propositions: Mapping[str, str], *, timeout_seconds:
float | None = None) -> DecisionResult[NoulAnswer]` with `NoulAnswer(probability: float)`,
verbatim from decision-classifier-path 3.1.

- Each proposition is sent as `{"type": "noul", "instructions": <proposition>}`.
- `probability` is read from the answer's `noul` field: finite and in [0,1], with no
  confidence.
- `noul` shares `choose`'s ceiling, cooldown, truncation guard, retry, identity and
  `decisions.call` log through one private request path, so each shape adds a body builder
  and a parser, never another transport.
- The request check accepts 1 to 64 non-empty propositions with valid keys.
- `_schema_hash` covers each question's type plus its option keys (Choice). Noul hashes
  its type alone.
- Before any code, author the three fixtures, each with three propositions:
  - the systemone fixture from the pinned schema;
  - the OpenRouter fixture with a dated `model`, `id`, `provider`, and `usage` carrying
    `output_tokens` and `cost`;
  - the Workers AI fixture inside the REST envelope.

Add the public `DecisionService.estimate_request_tokens(state, questions) -> int`. It
returns the estimate that the `oversize` guard compares with `max_input_tokens`, computed
on the same request body, so consumers can pack requests with it. In this leaf it accepts
a Choice map or a Noul proposition map.

Update `docs/evidence/decisions/wire-contracts.md`:

- Append the Noul fixture note.
- Relabel the narrower rules as Gobby's request profile over current upstream: required
  instructions, string-only descriptions, Noul as exactly `type` plus `instructions`, and
  the 2-option minimum. Current TypeSafe OpenAPI makes `instructions` optional, allows
  1 to 255 Choice options with string, object, array or null descriptions, and allows
  optional Noul `criteria.true` and `criteria.false`. Cite the working note's dated sources.

**Research context:**

- `DecisionService.choose` builds the body with `_request_body`, estimates it with
  `estimate_tokens` (`len(json) // 4`), and checks it with `_check_request` (1 to 64
  questions, key pattern `[A-Za-z0-9_.-]{1,100}`, 2 to 255 options). It dials through
  `_dial` and parses with `_parse_answers` and `_parse_choice`.
- `_parse_choice` requires the probabilities keys to equal the options, a sum within
  `_SUM_TOLERANCE`, and the choice to be the arg-max. `_unit` enforces finite [0,1].
- The tests use the `wire` fixture (`FakeWire`): an `httpx.MockTransport` monkeypatched
  over `httpx.AsyncClient`, with fixtures loaded through `_fixture` from
  `tests/ai/fixtures/`. The existing `*_choice_response.json` fixtures show each wire's
  envelope.
- The Noul wire facts are at wire-contracts.md section 1. The current upstream deltas are
  in the working note's "Score wire schema" section.
- Observed: `noul` does not exist.
- Planned: the three tests below, then `uv run ruff check src/` and `uv run mypy src/`.

**Acceptance:**

- 1.1.1 - `noul` posts per-proposition questions on every wire, parses the three documented-schema Noul fixtures, and returns probabilities by key under the same ceiling and cooldown. The fixture note is appended to `docs/evidence/decisions/wire-contracts.md`. test: `tests/ai/test_decisions_service.py::test_noul_returns_probabilities_by_key`.
- 1.1.2 - A Noul answer with the wrong `type`, a missing or non-finite `noul`, a value outside [0,1], or a missing key raises `DecisionsUnavailable` with reason `parse`. test: `tests/ai/test_decisions_service.py::test_noul_rejects_malformed_answers_as_parse`.
- 1.1.3 - `estimate_request_tokens` returns the estimate the `oversize` guard uses for the same state and questions, for Choice and Noul. test: `tests/ai/test_decisions_service.py::test_estimate_request_tokens_matches_oversize_guard`.
- 1.1.4 - The wire note labels the instructions, description, Noul-shape and option-count rules as Gobby policy over current upstream, with dated sources. behavior: "Gobby policy" in `docs/evidence/decisions/wire-contracts.md`.

### 1.2 `DecisionService.score` (depends: 1.1) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/ai/decisions.py::*` — scope-reason: add ScoreQuestion, ScoreAnswer, DecisionService.score and the strict Score parser, and extend the request check, body, estimate and schema hash to Score
- `tests/ai/test_decisions_service.py::*` — scope-reason: add the Score wire and malformed-answer tests
- `tests/ai/fixtures/systemone_score_response.json`
- `tests/ai/fixtures/openrouter_decisions_score_response.json`
- `tests/ai/fixtures/workers_ai_score_response.json`
- `docs/evidence/decisions/wire-contracts.md`

Add `ScoreQuestion(instructions: str, levels: tuple[str, ...])`, with
`ScoreAnswer(score: float, probabilities: dict[str, float], confidence: float)` and
`async score(consumer, state, questions: Mapping[str, ScoreQuestion], *, timeout_seconds:
float | None = None) -> DecisionResult[ScoreAnswer]`. All three run through 1.1's shared
request path.

- Request: each question is sent as `{"type": "score", "instructions": <non-empty
  string>, "criteria": [<level>, ...]}`, lowest level first. The request check enforces 2
  to 10 non-empty levels. Cloudflare and llama.cpp require at least 2 and TypeSafe's prose
  allows at most 10; TypeSafe's OpenAPI minimum of 1 and OpenRouter's missing maximum are
  looser.
- The parser applies Decision Record 6 in full:
  - all five fields are present;
  - `legend` keys and `probabilities` keys both equal `"0"` to `"n-1"`;
  - each probability is in [0,1] and they sum to 1 within `_SUM_TOLERANCE`;
  - `score` is in [0, n-1] and equals the probability-weighted index within
    (n-1) x `_SUM_TOLERANCE`;
  - `confidence` is in [0,1].
  Anything else raises `parse`.
- `ScoreAnswer` drops `legend` after validating it.
- `_schema_hash` adds the level count for Score.
- `estimate_request_tokens` accepts a Score map.
- Before any code, author three fixtures from the transcribed schemas, each with three
  questions of four levels:
  - systemone: TypeSafe, all five fields;
  - OpenRouter: the dated `model`, `id`, `provider`, `usage.cost`, and all five fields;
  - Workers AI: the REST envelope.
- Append a Score section to `wire-contracts.md` with:
  - the request and answer profile;
  - the retrieval date 2026-10-24 and the sources from the working note;
  - the per-backend differences: OpenRouter's optional answer fields fail the strict
    profile, and the TypeSafe prose and schema disagree on the level minimum.

**Research context:**

- Transcription: the working note's "Score wire schema" section. It came from researcher
  gobby#17515 (message 5b02c76a), from TypeSafe OpenAPI 0.2.0, OpenRouter
  `openapi.json` and its SDK, Cloudflare `clef.json`, and llama.cpp's server README, which
  LM Studio delegates to.
- With n levels, the documented score is the expected index `sum(i * p_i)`, in [0, n-1].
- Decision 10 of decision-classifier-path: weighted score is never compared with
  probability or confidence across primitives.
- Observed: no Score code or fixture exists.
- Planned: the two tests below, then ruff and mypy on `src/`.

**Acceptance:**

- 1.2.1 - `score` posts ordered levels on every wire, parses the three Score fixtures, and returns score, probabilities and confidence by key under the shared ceiling and cooldown. test: `tests/ai/test_decisions_service.py::test_score_returns_weighted_levels_by_key`.
- 1.2.2 - A missing `legend`, `probabilities` or `confidence`, a key set other than `"0"` to `"n-1"`, a probability sum outside tolerance, a score outside [0, n-1], or a score inconsistent with the probability-weighted index raises `parse`. Fewer than 2 or more than 10 levels is rejected before dialing. test: `tests/ai/test_decisions_service.py::test_score_rejects_malformed_answers_as_parse`.
- 1.2.3 - The wire note pins the Score profile with its retrieval date and the per-backend differences. behavior: "score" in `docs/evidence/decisions/wire-contracts.md`.

### 1.3 `DecisionService.box` (depends: 1.2) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/ai/decisions.py::*` — scope-reason: add BoxImage, BoxQuestion, BoxAnswer, DecisionService.box, the image boundary check, the per-wire image transport and the strict Box parser
- `tests/ai/test_decisions_service.py::*` — scope-reason: add the Box wire, transport and malformed-answer tests
- `tests/ai/fixtures/systemone_box_response.json`
- `tests/ai/fixtures/openrouter_decisions_box_response.json`
- `docs/evidence/decisions/wire-contracts.md`

Box's consumer is #24248's evaluate tool (Decision Record 9). This leaf delivers the
service method that the tool calls.

Add the types and the method:

- `BoxImage(mime_type: Literal["image/png", "image/jpeg"], data_base64: str)`.
- `BoxQuestion(instructions: str, image: BoxImage)`.
- `BoxAnswer(top: float, bottom: float, left: float, right: float, confidence: float)`.
- `async box(consumer, state, questions: Mapping[str, BoxQuestion], *, timeout_seconds:
  float | None = None) -> DecisionResult[BoxAnswer]`, through the shared request path.

Boundary check, before dialing, so a bad image never opens the shared cooldown with a
permanent 4xx:

- Instructions are non-empty.
- The decoded bytes start with the PNG or JPEG signature for the declared type.
- Each image is at most `gobby.llm.image_payloads.MAX_IMAGE_BYTES`. A request carries at
  most `MAX_REQUEST_IMAGES` images and `MAX_REQUEST_IMAGE_BYTES` in total.
- Each dimension is 280 to 1568 pixels, and the area is 78,400 to 2,458,624. These are
  TypeSafe's documented limits, read from the PNG IHDR or JPEG SOF header with the
  standard library.
- A failure raises `invalid_request`.

Per-wire transport, so no wire ever gets another wire's image payload:

- `openrouter-decisions`: `{"type": "box", "instructions": [<instructions>, {"type":
  "image_url", "image_url": {"url": "data:<mime>;base64,<data>"}}]}`. OpenRouter ignores a
  top-level `assets` map and answers 400 to a top-level `images` field.
- `systemone`: `{"type": "box", "instructions": [<instructions>, <ref>]}`, plus a
  top-level `assets` entry `{<ref>: {"type": "image", "format": "png"|"jpeg",
  "data_base64": <data>}}` and the header `TypeSafe-Danger-Zone-Images: true`. `<ref>` is
  `gobby-image-` followed by a fresh `uuid4` hex. A whole-value reference avoids substring
  matches.
- `workers-ai`: Box is absent from Clef's schema, so `box` raises `invalid_request`
  before dialing.

Estimate and guard:

- `estimate_request_tokens` counts the body with each image's data replaced by an empty
  string. Image token cost is unknown until the capture.
- The `oversize` guard uses that estimate. The truncation guard on `usage.input_tokens`
  still applies.

The parser applies the strict profile:

- `type` is `box`;
- `box` carries finite `top`, `bottom`, `left` and `right`, each in [0,1], with
  `left < right` and `top < bottom`;
- `confidence` is present and finite in [0,1].

Missing or null `confidence` raises `parse`, though OpenRouter marks it optional and
TypeSafe allows null. The service never invents a confidence. There is no sum check.

Before any code, author the systemone fixture (TypeSafe) and the OpenRouter fixture
(model `typesafe/jev-1.14-20261014`, `usage.cost`), each with two questions. Append a Box
section to `wire-contracts.md` with:

- the profile and both transports;
- the image limits;
- Jev 1.14 as a requirement;
- the retrieval date 2026-10-24;
- the documented absence of Box on Workers AI and llama.cpp or LM Studio.

**Research context:**

- Transcription: the working note's "Box wire schema" section, from researcher
  gobby#17515 (messages 490d43e6 and 29f11f35).
- TypeSafe matches asset references against whole string values, then substrings, so a
  short ref like `cat` also matches inside `category`. Every asset must be referenced, or
  the request returns 400.
- OpenRouter's multimodal guide documents image parts inside instructions arrays for Jev.
  OpenRouter's 2026-10-16 article reports a real Box answer from
  `typesafe/jev-1.14-20261014`, with confidence 0.95.
- `src/gobby/llm/image_payloads.py` holds the size constants and `prepare_image_data`
  (absolute path or data URL to base64), which #24248 reuses for Box input.
- Pillow is not a direct dependency, so the dimension read uses the standard library.
- Observed: no Box code exists.
- Planned: the three tests below, then ruff and mypy on `src/`.

**Acceptance:**

- 1.3.1 - `box` sends the image as an inline `image_url` part on `openrouter-decisions` and as a referenced `assets` entry with the danger-zone header on `systemone`, refuses `workers-ai` before dialing, and returns edges and confidence by key from both fixtures. test: `tests/ai/test_decisions_service.py::test_box_returns_edges_by_key_per_wire_transport`.
- 1.3.2 - A non-PNG/JPEG payload, an oversized image or request, or dimensions outside the documented limits raise `invalid_request` before dialing and leave the cooldown closed. test: `tests/ai/test_decisions_service.py::test_box_rejects_invalid_images_before_dialing`.
- 1.3.3 - Edges outside [0,1], `left >= right`, `top >= bottom`, a missing or null `confidence`, or a wrong `type` raise `parse`. test: `tests/ai/test_decisions_service.py::test_box_rejects_malformed_answers_as_parse`.
- 1.3.4 - The wire note pins the Box profile, both transports, the image limits and the unsupported wires with the retrieval date. behavior: "box" in `docs/evidence/decisions/wire-contracts.md`.

## P2: Community-label consumers
`kind: framing`

### 2.1 Choice gate on generated names (depends: 1.1) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/code_index/community_labeler.py::*` — scope-reason: add the decisions resolver, the effective-mode rule, request packing, the Choice gate and the four new outcomes, and report write_superseded on every write path
- `src/gobby/runner_startup_code_index.py::_start_code_index_tasks`
- `tests/code_index/test_community_labeler.py::*` — scope-reason: add the gate, discriminator, packing and evaluation-mismatch tests with a stubbed decisions wire
- `tests/config/test_runtime_loop_consumers.py::*` — scope-reason: prove startup wires the labeler to the live decisions config

`CommunityLabeler.__init__` gains a keyword `decisions_resolver: Callable[[],
DecisionsConfig | None] | None = None`. `_start_code_index_tasks` passes `lambda:
runner.config_runtime.capture().snapshot.active.ai.decisions`, the same capture pattern as
`capture_code_index`. `maintenance.py::_label_unlabeled_communities` keeps calling
`label_batch` unchanged.

Off semantics, per consumer:

- A `None` resolver, a `None` config, or an unset `api_base` or `model` turns every
  decision stage off.
- Otherwise each stage reads only its own `mode`. An `off` stage makes no decision
  request and passes its rows to the next stage unchanged. The other stages still run.
- With every stage off, `label_batch` behaves exactly as 6.2 and sends no HTTP request.
- In this leaf the gate is the only stage. 2.2 and 2.3 add theirs under the same rule.

Gate inputs:

- After generation, the gate covers only communities whose generated name passes
  `sanitize_community_label`. A name equal to a candidate stays `written_deterministic`,
  and a failed name keeps its 6.2 outcome.
- One Choice question per community, keyed `c<community_id>`. Its instructions name that
  key. `criteria` maps `generated` to the sanitized name, and each `deterministic_<i>` to
  `label_candidates[i]`, giving 2 to 5 options.
- Shared state is `{"communities": {"c<id>": {"representatives", "members", "candidates",
  "generated": {"name", "rationale"}}}}`. Members are capped at 40, representatives first,
  as `_label_prompt` does. The answer can only name an offered key, and the parser
  enforces that, so the state needs no prompt sanitization.

Packing:

- Pack questions greedily in batch order while there are at most 64 and
  `service.estimate_request_tokens(state, questions)` stays within
  `config.max_input_tokens`. Send the requests one after another.
- A community that cannot fit alone gets `DecisionsUnavailable("oversize")`, handled as
  below.

Effective mode follows Decision Record 11, through one private helper that the carry and
sufficiency checks reuse. It warns once per `failure_cooldown_seconds`.

Enforce outcomes:

- `generated` with `confidence >= min_confidence` is `gate_admitted`. It writes the
  sanitized name with `label_source="model"`, `label_confidence=confidence`,
  `label_model=result.response_model` and `labeled_signature`.
- `deterministic_<i>` is `gate_rejected`. It writes `label_candidates[i]` with
  `label_source="deterministic"`, the confidence, the response model and the signature.
- `generated` below the threshold is `gate_rejected`. It writes `label_deterministic`
  with the same fields.
- `DecisionsUnavailable` is `transport_failed`, which stamps the attempt only.

Shadow writes exactly what 6.2 writes. The outcome event gains `decision_mode`,
`decision_choice`, `decision_confidence` and `decision_unavailable`.

Every write path whose signature-guarded `update_community_label` returns `False` reports
`write_superseded` and stamps nothing. This replaces today's silent `continue`. The
discriminator has exactly nine values.

**Research context:**

- `label_batch(project_id, communities, *, storage, run_db)` generates, sanitizes and
  writes. `_write_label` returns `None` when the guarded write loses. It logs the debug
  event `code_index.community_label.outcome` (`project_id`, `community_id`,
  `member_signature`, `outcome`) and one info line, `code_index.community_label.batch`.
- `CommunityLabelOutcome` has five values today.
- `label_candidates` holds up to four distinct strings, led by the deterministic label
  (gcode-import-communities 2.2.9).
- `ChoiceConsumerConfig.min_confidence` defaults to 0.5. Enforce requires
  `evaluated_model` and `evaluated_backend` (`_DecisionConsumerConfig`).
- Test pattern: `tests/ai/test_decisions_service.py`'s `wire` fixture monkeypatches
  `httpx.AsyncClient` with an `httpx.MockTransport`. The labeler tests define the same
  stub locally and build a real `DecisionService` through `get_decision_service`, so the
  path from wire to stored row is real.
- `test_every_outcome_logs_at_debug_under_one_info_batch_summary` stays untouched, so each
  leaf's acceptance names its own symbol.
- Observed: `CommunityLabeler(runner.text_generation_service, config.code_index)` is the
  only construction site, and `tests/config/test_runtime_loop_consumers.py` already
  proves other lifecycle consumers read the runtime capture.
- Planned: the five tests below, then ruff and mypy.

**Granularity:** This section carries six acceptance items over two production files.
The gate, its outcomes, packing and the mode rule are one state transition per community,
and splitting them would leave a gate that writes without its outcome contract.

Consumers unchanged:
- `src/gobby/runner_lifecycle_subsystems.py` — no-edit-reason: it calls `_start_code_index_tasks(runner, tracker)`, whose signature is unchanged.
- `src/gobby/code_index/maintenance.py` — no-edit-reason: `_label_unlabeled_communities` calls `label_batch` with its unchanged signature.
- `tests/test_runner_lifecycle.py` — no-edit-reason: it patches `_start_code_index_tasks` by name, and the name and signature are unchanged.
- `tests/test_runner_lifecycle_subsystems.py` — no-edit-reason: it records `_start_code_index_tasks` by name, and the name and signature are unchanged.

**Acceptance:**

- 2.1.1 - A `generated` choice at or above the threshold writes the model label with confidence and model; anything else writes the deterministic label with confidence, model, and signature stamped; transport failure stamps the attempt only. test: `tests/code_index/test_community_labeler.py::test_gate_admits_at_threshold_and_rejects_below`.
- 2.1.2 - The outcome event's discriminator admits exactly nine values, the five from 6.2 plus `gate_admitted`, `gate_rejected`, `transport_failed`, and `write_superseded`, where `write_superseded` stamps no field, and the exhaustiveness test covers all nine. test: `tests/code_index/test_community_labeler.py::test_gate_outcomes_extend_the_discriminator_to_nine`.
- 2.1.3 - Requests carry at most 64 questions and stay within `max_input_tokens` by `estimate_request_tokens`, and a batch larger than one request is split across sequential requests. test: `tests/code_index/test_community_labeler.py::test_gate_batches_fit_the_request_ceiling`.
- 2.1.4 - `enforce` without a matching `evaluated_model` and `evaluated_backend` runs as shadow, writes 6.2's result, warns once per cooldown window, and a mode change takes effect on the next batch. test: `tests/code_index/test_community_labeler.py::test_enforce_without_matching_evaluation_runs_as_shadow`.
- 2.1.5 - Startup constructs the labeler with a resolver that reads `ai.decisions` from the live runtime capture on each call. test: `tests/config/test_runtime_loop_consumers.py::test_community_labeler_reads_live_decisions_config`.
- 2.1.6 - With the gate `off`, or with the decisions config unset, the labeler sends no decision request and writes 6.2's result. test: `tests/code_index/test_community_labeler.py::test_gate_off_makes_no_decision_request`.

### 2.2 Noul carry check for stale model names (depends: 2.1) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/code_index/community_labeler.py::*` — scope-reason: add the Noul carry check ahead of generation and its event
- `src/gobby/config/ai.py::*` — scope-reason: add DecisionsConfig.community_label_carry, reusing RerankConsumerConfig
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerate the derived runtime config contract for the new consumer field
- `tests/contracts/http/config_schema.json::*` — scope-reason: add the community_label_carry schema entry
- `tests/contracts/http/config_values.json::*` — scope-reason: add the community_label_carry default values
- `docs/audits/configuration-audit.md`
- `tests/config/test_decisions_config.py::*` — scope-reason: add the consumer to CONSUMERS and the default-mode assertion
- `tests/code_index/test_community_labeler.py::*` — scope-reason: add the carry live-path and fallback tests

Add `DecisionsConfig.community_label_carry: RerankConsumerConfig`, reusing the class: a
probability floor with the same 0.5 default. Then:

- Regenerate `runtime_config_contract.json` with
  `uv run python scripts/generate_runtime_config_contract.py`.
- Update both HTTP contract fixtures.
- Update the `ai.decisions` row note in `configuration-audit.md`.
- Extend `CONSUMERS` and the `["off"] * 3` assertion in
  `test_decisions_config_invariants`. Commit
  635f7c8417 shows the complete carrier set.

Before generation, `label_batch` selects stale model rows: `label_source == "model"` and
`labeled_signature is not None` and `labeled_signature != member_signature`.

- Each stale row's label is re-sanitized against the current members first. A failure
  falls through to generation.
- The Noul proposition is: "The name `<label>` describes what the files in community
  c<id> have in common." It is keyed `c<id>`, with the current members and representatives
  as state, and packed the way 2.1 packs.
- In effective enforce, `probability >= min_probability` calls `update_community_label`
  with the same label, source, confidence and model, and
  `labeled_signature=member_signature`. Generation is skipped.
- Below the threshold, in shadow, or on `DecisionsUnavailable`, the row goes through
  generation and the 2.1 gate.
- The event `code_index.community_label.carry` records `outcome`
  (`kept|regenerated|superseded|unavailable`), `mode` and `probability`. `superseded`
  means the guarded update lost.

**Research context:**

- Carry-forward (gcode-import-communities 2.3) keeps a matched community's prior model
  label, confidence, model and old `labeled_signature`, so the row is stale and re-enters
  `get_unlabeled_communities` because `labeled_signature IS DISTINCT FROM
  member_signature`. Readers show the deterministic label while it is stale.
- `update_community_label(... label, label_source, label_confidence, label_model,
  labeled_signature) -> bool` is signature-guarded and reused unchanged.
- `RerankConsumerConfig.min_probability` defaults to 0.5.
- Derived-carrier rule: any `config/*.py` Target requires `runtime_config_contract.json`.
- Observed: `CONSUMERS` lists three consumers.
- Planned: the two tests below, `tests/config/test_decisions_config.py`, the HTTP contract
  tests, then ruff and mypy.

**Acceptance:**

- 2.2.1 - In enforce, a stale model name whose Noul probability is at or above `min_probability` keeps its label, source, confidence and model, gets the current signature, and skips generation. test: `tests/code_index/test_community_labeler.py::test_carry_keeps_name_above_threshold_without_generation`.
- 2.2.2 - A probability below the threshold, shadow mode, an unavailable decision, or a name that fails re-sanitization sends the row through generation and the gate, and the carry event records the outcome. test: `tests/code_index/test_community_labeler.py::test_carry_below_threshold_or_shadow_regenerates`.
- 2.2.3 - `ai.decisions.community_label_carry` defaults to mode `off` with `min_probability` 0.5. test: `tests/config/test_decisions_config.py::test_decisions_config_invariants`.
- 2.2.4 - With the gate `off` and carry in enforce, the carry check still keeps or regenerates stale names, and the gate sends no request. test: `tests/code_index/test_community_labeler.py::test_carry_runs_with_gate_off`.

### 2.3 Score sufficiency check for deterministic names (depends: 1.2, 2.2) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/code_index/community_labeler.py::*` — scope-reason: add the Score sufficiency check ahead of generation and its event
- `src/gobby/config/ai.py::*` — scope-reason: add ScoreConsumerConfig and DecisionsConfig.community_label_sufficiency
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: regenerate the derived runtime config contract for the new consumer class and field
- `tests/contracts/http/config_schema.json::*` — scope-reason: add the community_label_sufficiency schema entry
- `tests/contracts/http/config_values.json::*` — scope-reason: add the community_label_sufficiency default values
- `docs/audits/configuration-audit.md`
- `tests/config/test_decisions_config.py::*` — scope-reason: add the consumer to CONSUMERS and the min_score bounds
- `tests/code_index/test_community_labeler.py::*` — scope-reason: add the Score live-path and fallback tests

Add `ScoreConsumerConfig(_DecisionConsumerConfig)` with `min_score: float = Field(0.5,
ge=0.0, le=1.0)`. Its docstring says the field is the minimum position `score / (levels -
1)`. Add `DecisionsConfig.community_label_sufficiency: ScoreConsumerConfig`, with the same
carrier set as 2.2.

After the carry check, every remaining row in the batch gets one Score question, keyed
`c<id>`:

- Instructions: "Rate how well the name `<label_deterministic>` describes what the files
  in community c<id> have in common."
- Levels, lowest first: `misleading: the name points at the wrong purpose`, `vague: the
  name fits but says little`, `clear: the name says what the files share`, `clear and
  specific: the name says what the files share and how they differ from their neighbors`.
- State and packing follow 2.1.

Effective mode follows Decision Record 11:

- In enforce, position `>= min_score` writes `label_deterministic` with
  `label_source="deterministic"`, `label_confidence=None`,
  `label_model=result.response_model` and `labeled_signature`, and skips generation.
  `label_confidence` stays `None`, because that column holds Choice confidence and the
  Score position is never compared with it (Decision 10).
- Below the threshold, in shadow, or on `DecisionsUnavailable`, the row goes to
  generation.

The event `code_index.community_label.sufficiency` records `outcome`
(`sufficient|insufficient|superseded|unavailable`), `mode` and `position`.

**Research context:**

- `label_deterministic` is always set on a stored community.
- The CHECK on `code_communities` permits `label_model` on a deterministic row, so
  2.1's `gate_rejected` writes it too.
- Score answers come from 1.2.
- Ordering inside `label_batch`: carry first, then sufficiency, then generation, so a
  kept stale name is never re-scored.
- Observed: no `ScoreConsumerConfig` exists.
- Planned: the two tests below and the config tests, then ruff and mypy.

**Acceptance:**

- 2.3.1 - In enforce, a deterministic name whose Score position is at or above `min_score` is written with the response model and signature, and generation is skipped. test: `tests/code_index/test_community_labeler.py::test_sufficient_deterministic_label_skips_generation`.
- 2.3.2 - A position below the threshold, shadow mode, or an unavailable decision sends the row to generation, and the sufficiency event records the outcome and position. test: `tests/code_index/test_community_labeler.py::test_insufficient_or_shadow_score_generates`.
- 2.3.3 - `ai.decisions.community_label_sufficiency` defaults to mode `off` with `min_score` 0.5 and rejects values outside [0,1]. test: `tests/config/test_decisions_config.py::test_score_consumer_bounds_min_score`.
- 2.3.4 - Sufficiency in enforce runs with the gate and carry `off`, and with all three stages `off` a batch sends no HTTP request. test: `tests/code_index/test_community_labeler.py::test_sufficiency_runs_alone_and_all_off_sends_nothing`.

### 2.4 Community-label decision docs (depends: 2.3) [category: docs]
`kind: deliverable`

Targets:
- `docs/guides/llm-features.md`

Add the row `code_index.community_label` next to the `code_index.symbol_summary` row,
before "## Outside Feature Helpers". Add one paragraph on the three daemon decision checks
in order: carry (Noul), sufficiency (Score), and the gate (Choice). Name their
`ai.decisions` keys, the defaults (`off`, 0.5), the effective-enforce rule and the three
events. The gate is not an `AICapability` and not a text-generation profile route. Point
at `docs/evidence/community-labels-2026-09/label-quality.md` for the ship decision.

**Research context:**

- `docs/guides/llm-features.md` holds the `code_index.symbol_summary` row near the end of
  its feature table. No `community_label` row exists.
- #24249 edits this file later, in different rows (Coordination).
- Planned: a markdown review against the shipped config names.

**Acceptance:**

- 2.4.1 - `docs/guides/llm-features.md` documents `code_index.community_label` and the decisions gate, with the carry and sufficiency checks, their keys, defaults and events. behavior: "community_label" in `docs/guides/llm-features.md`.

## P3: Live evidence and ship decision
`kind: framing`

### 3.1 Live capture script (depends: 1.3) [category: code]
`kind: deliverable`

Targets:
- `scripts/decisions_live_capture.py`
- `tests/scripts/test_decisions_live_capture.py`

`uv run python scripts/decisions_live_capture.py` takes these flags: `--wire-api`,
`--api-base`, `--model`, `--identity-contract`, `--backend-max-state-tokens`,
`--api-key-secret NAME`, `--alias`, `--box-image PATH` and `--out PATH`.

- It builds its own `DecisionsConfig` and never reads the live config.
- `--api-key-secret` resolves through `SecretStore.get`, so only the operator can run it.
- Through `DecisionService` it runs one round trip each, in this order: Choice, Noul,
  Score, then Box. Each shape gets a fresh `DecisionService(config)`, so a failure's
  cooldown cannot mask a later shape. Box uses the PNG at `--box-image`, prepared by
  `prepare_image_data`.
- With raw `httpx` it probes:
  - the chars-per-token ratio from `usage.input_tokens` on known-length states;
  - one request just under `--backend-max-state-tokens` and one over it;
  - an alias check: `--alias` against the pinned model, comparing `response.model`.
- It records `usage.cost` where present.
- On `openrouter-decisions` it also reads `canonical_slug` from `GET
  https://openrouter.ai/api/v1/models?output_modalities=decisions`, and checks that every
  request carried `provider: {"zdr": true, "data_collection": "deny"}`.
- A shape that fails records its `DecisionsUnavailable` reason and detail as a finding.
  The script never exits early on one.
- It writes a Markdown section dated by the run, with the request shapes, response models,
  backend identity, usage, ratios and findings. It never writes the key or image bytes.

Tests stub the wire with `httpx.MockTransport` and a fake secret resolver. They prove the
section's contents, the per-shape findings, and that the key never appears in the output.

**Research context:**

- Activation Gate item 4 (decision-classifier-path, section "Activation Gate and #22075")
  requires, per capture:
  - backend identity under the contract;
  - a Choice and a Noul round trip, and now a Score round trip and a Box attempt;
  - the chars-per-token ratio;
  - the input limit and its scope;
  - for `response_version`: the model pinned, the alias check, the documented limits,
    errors and usage cited by URL and date, and live just-under and over probes;
  - for OpenRouter: `canonical_slug` and the privacy flags.
- `tests/scripts/` has `__init__.py`, and scripts import as `from scripts import ...`
  (`tests/scripts/test_flatten_schema.py`).
- `SecretStore.get(name, *, project_id=None)` is in `src/gobby/storage/secrets.py`.
- Planned: the two tests below, then ruff on `scripts/`.

**Acceptance:**

- 3.1.1 - One run writes a dated capture section with identity, a result or finding for each of Choice, Noul, Score and Box, the token ratio, both limit probes, the alias check and usage. test: `tests/scripts/test_decisions_live_capture.py::test_capture_records_every_shape_and_probe`.
- 3.1.2 - The resolved key and image bytes never appear in the written section, and the live config is never read. test: `tests/scripts/test_decisions_live_capture.py::test_capture_output_omits_key_and_image`.

### 3.2 Live captures per wire (depends: 3.1) [category: docs]
`kind: deliverable`

Targets:
- `docs/evidence/decisions/openrouter-decisions-live.md`
- `docs/evidence/decisions/systemone-live.md`
- `docs/evidence/decisions/wire-contracts.md`

Josh runs two prepared commands, which the developer sends through Triage and the
Assistant relays:

- OpenRouter with `typesafe/jev-1.14`, `identity_contract: response_version`, and Josh's
  stored key name.
- LM Studio at `http://127.0.0.1:1234` with Josh's chosen model id (default
  `Clef-Flash-Q8_0.gguf`), `identity_contract: model_card`, and
  `--backend-max-state-tokens` set to the loaded context length.

The developer commits each output as a dated section. A later capture appends a new
section. Each file closes with an item-4 checklist that marks every item met or unmet.

Update `wire-contracts.md`'s unverified table with what the captures settled, including
whether OpenRouter's Score and Box answers carry the fields the strict profile requires.

If LM Studio fails item 4, it stays `shadow` or `off`. That does not block 3.4. If
OpenRouter fails item 4, no consumer may enforce, so 3.4 ships with every consumer `off`.

**Research context:**

- #22604's substituted 6.3.6 requires one capture per wire under
  `docs/evidence/decisions/` before either is enabled.
- Observed: no capture exists.
- Planned: the two operator runs, then review that each file's checklist matches item 4.

**Acceptance:**

- 3.2.1 - The OpenRouter capture records identity, all four shapes, both probes, the alias check, `canonical_slug`, the privacy flags and the item-4 checklist. behavior: "canonical_slug" in `docs/evidence/decisions/openrouter-decisions-live.md`.
- 3.2.2 - The LM Studio capture records the model id, the card identity result, all four shapes, both probes and the item-4 checklist. behavior: "item 4" in `docs/evidence/decisions/systemone-live.md`.
- 3.2.3 - The wire note's unverified table reflects both captures. behavior: "captured" in `docs/evidence/decisions/wire-contracts.md`.

### 3.3 Label-quality study tooling (depends: 2.3, 3.1) [category: code]
`kind: deliverable`

Targets:
- `scripts/community_label_study.py`
- `tests/scripts/test_community_label_study.py`

`uv run python scripts/community_label_study.py prepare` reads `code_communities` for
projects `gobby` (`d45545c5-ded5-4335-b115-0245752edacf`) and C1
(`c8e071af-e1f7-434f-b3c0-f31a1b1299c9`), read-only.

Sampling:

- Eligible communities have `member_count >= 3`.
- Per corpus, the script stratifies into size terciles and draws 5 per tercile with seed
  22604, giving 15 per corpus and 30 in total.
- Fewer than 15 eligible in either corpus exits nonzero with the counts and writes no
  sheet.

Arms. The script runs `CommunityLabeler.label_batch` with an in-memory recording storage,
so no live row changes, and a text-generation wrapper that memoizes per community id and
`member_signature`, so G and J see identical generations. Each sampled community is
copied in memory with `label`, `label_source` and `labeled_signature` cleared, so every
one goes through generation, and carry stays `off` in the G and J arms.
`prepare --no-decisions` runs D and G only and records why. The developer uses it when
3.2's OpenRouter checklist has any unmet item.

- **D** is the stored `label_deterministic`.
- **G** runs the gate `off`.
- **J** runs the gate in enforce on OpenRouter. `evaluated_model` is the exact request
  model `config.model` (`typesafe/jev-1.14`). `evaluated_backend` is a probe decision's
  `backend_identity` (`response:<dated response model>`). The run record keeps the probe's
  full request and response pair. If any J batch logged `evaluated_backend_mismatch` or
  otherwise ran as shadow, the run aborts: the backend changed mid-run.
- **Sufficiency** runs in shadow over the same 30 communities and records each position.
- **Carry** draws no new communities. It reuses the 30 main communities and makes one
  perturbed in-memory copy of each community whose G arm wrote a model name. The run
  record counts the communities skipped because G wrote none.
  - A copy keeps G's written `label`, `label_source="model"`, `label_confidence` and
    `label_model`, and sets `labeled_signature` to the original `member_signature`. Its
    own `member_signature` is the original plus the suffix `:carry`, so the G name is
    stale.
  - A copy's position is its community's slot, 1 to 15, in that corpus's sampling order,
    not its rank among the copies. Copies at odd positions (1st, 3rd, and so on) get a
    mild perturbation. They drop `max(1, floor(member_count / 10))` members,
    chosen with seed 22604 from members not in `representatives`. If too few exist, the
    rest come from the remaining members.
  - Copies at even positions get a heavy perturbation. They replace
    `floor(member_count / 2)` members, chosen with the seed, with as many members drawn
    with the seed from the next community in that corpus's order (the 15th wraps to the
    1st). The draw skips members the copy already has. If the next community has fewer
    such members, the copy replaces only that many.
  - `representatives` keeps the original representatives that remain members, and
    `member_count` is the new member count.
  - The carry arm runs with the gate `off`, sufficiency `off`, and carry in enforce with
    the J arm's evaluated pair. A carry batch that ran as shadow aborts the run, as in J.
    It records each copy's carry outcome and Noul probability. Names regenerated for
    copies go in the key file but are not rated.

Outputs:

- One blinded CSV sheet with columns `item_id, members, name, accuracy (0-2),
  findability (0-2)`. Each main community gives one item per distinct name among its D,
  G and J names, with the original members. Each carry copy gives exactly one item: the
  pre-perturbation G name against the post-perturbation members, whether the copy was
  kept or regenerated. A carry item is never merged with a main item. Items are shuffled
  with the seed, giving at most 120.
- A separate key file mapping `item_id` to corpus, community, arm (`D`, `G`, `J` or
  `carry`) and decision fields: the recorded `label_confidence`, plus the perturbation,
  carry outcome and Noul probability for a carry item.
- A run record with outcome-event counts and decision usage.

`uv run python scripts/community_label_study.py score --sheet PATH --key PATH --capture
PATH` reads Josh's completed sheet and computes:

- every Q1.6 bar, plus the carry and sufficiency bars, with the values under "Bars and
  ship rules" below;
- the decision cost: the sum of `estimated_tokens x (4 / measured chars-per-token) x
  (usage.cost / usage.input_tokens)`, with both ratios taken from the OpenRouter capture.

It writes the results section of `label-quality.md`, including the ship configuration
those rules select. A sheet with an empty or out-of-range rating exits nonzero and names
the item.

Metric definitions. Every metric is a total function, and an undefined value fails its
bar.

- Ratings: each item has accuracy `a` and findability `f` in {0, 1, 2}, and total
  `t = a + f`. Deduplicated names share one item, so they share its rating.
- Arm names per community:
  - D is `label_deterministic`.
  - G and J are the label their arm wrote. Where the arm wrote nothing (a failed
    generation, schema or sanitation rejection, or `transport_failed`), the name is
    `label_deterministic`, which is what readers show.
- Lift: mean `a(J) - a(D)` and mean `f(J) - f(D)` over all 30 communities.
- Regression: the count of communities with `t(J) < t(D)`, or `t(G) < t(D)` for rule 1.
  It passes at 1 or fewer (5% of 30).
- Gate population: communities with a `gate_admitted` or `gate_rejected` outcome. A
  positive is `t(G) >= t(D)`. The predicted positive is `gate_admitted`.
  - Precision is TP divided by predicted positives.
  - Recall is TP divided by actual positives.
  - A zero denominator fails the bar.
- Confidence separation: the mean recorded `label_confidence`, which is the Choice
  `confidence` that 2.1 writes on both paths, over the J arm's `gate_admitted`
  communities, minus the same mean over its `gate_rejected` ones. An empty admitted or
  rejected set fails the bar.
- Schema-rejection rate: `schema_rejected` outcomes divided by the 30 generation attempts
  in the G arm, which shares its generations with J.
- Carry population: carry copies with a `kept` or `regenerated` outcome. A positive is
  the copy's item, the old name against the new members, rated `a = 2`. The predicted
  positive is `kept`. Zero denominators fail the bar.
- Sufficiency population: the 30 main communities with a Score answer. A positive is
  `t(D) >= t(G)`. The predicted positive is `position >= min_score`. Zero denominators fail
  the bar.
- Cost: the formula above, summed over every decision request in the run.

Bars and ship rules. `score` applies these exact values, and 3.4 reports them:

- Gate bars (Q1.6):
  - lift of at least 0.3 mean accuracy and 0.5 mean findability;
  - J regression on at most 1 of 30 communities;
  - gate precision >= 0.85 and recall >= 0.60;
  - confidence separation >= 0.15;
  - schema-rejection rate <= 10%;
  - decision cost <= $0.05.
- Carry bars: precision >= 0.85 and recall >= 0.60.
- Sufficiency bars: precision >= 0.85 and recall >= 0.60.

Ship rules, in order:

1. If G regresses on at most 1 of 30 communities, the gate ships `off`.
2. Otherwise, if J passes every gate bar, `ai.decisions.community_label` ships
   `mode: enforce` with the study's `evaluated_model` and `evaluated_backend`.
3. Otherwise D ships, and this branch is terminal (D5). The config is
   `code_index.community_label.enabled: false`, and `ai.decisions.community_label`,
   `community_label_carry` and `community_label_sufficiency` all ship `mode: off`.

Under rule 1 or rule 2 only, `community_label_carry` and `community_label_sufficiency`
each ship `mode: enforce` with the same evaluated pair if they pass their own bars, and
`mode: off` otherwise.

A `--no-decisions` run has no J, carry or sufficiency data, so rule 2 is unreachable and
every consumer ships `mode: off`. Rule 1 or rule 3 then decides `enabled`.

**Research context:**

- Q1.6 (gcode-import-communities.md, Phase-0 question 6) defines the study: 30
  communities, 15 per corpus, size-stratified; arms D, G and J; blind accuracy and
  findability ratings.
- `StoredCommunity` (`src/gobby/code_index/models.py`) carries members, representatives,
  `label_candidates` and `label_deterministic`. The study never reads stored `label` or
  `label_source` (Coordination).
- Reuses 3.1's config builder and secret resolution by import.
- Planned: the four tests below, then ruff on `scripts/`.

**Granularity:** This section has six acceptance items over two new files. Sampling, arm
execution, blinding and scoring share one data model (the key file), and the scoring step
has no independent use, so splitting them would add a file contract with no new behavior.

**Acceptance:**

- 3.3.1 - `prepare` draws 15 size-stratified communities per corpus with seed 22604 and refuses with counts when either corpus has fewer than 15 eligible. test: `tests/scripts/test_community_label_study.py::test_prepare_stratifies_and_refuses_short_corpus`.
- 3.3.2 - G and J share memoized generations, J and carry run in effective enforce, and a J or carry batch that ran as shadow aborts the run; no live row changes. test: `tests/scripts/test_community_label_study.py::test_arms_share_generations_and_abort_on_shadow`.
- 3.3.3 - The sheet is blinded and deduplicated, and the key file is separate. Carry reuses the 30 main communities, with the seeded mild and heavy perturbations at odd and even positions. Each carry copy contributes one item, the pre-perturbation G name against the post-perturbation members, for both kept and regenerated outcomes. test: `tests/scripts/test_community_label_study.py::test_sheet_is_blinded_and_key_is_separate`.
- 3.3.4 - `score` computes every bar and the decision cost from the sheet, key and capture, fails a bar on any undefined metric (zero denominators, empty admitted or rejected sets), and rejects an incomplete sheet. test: `tests/scripts/test_community_label_study.py::test_score_computes_bars_and_rejects_incomplete_sheet`.
- 3.3.5 - With an alias request model and a dated response model, the J arm runs in effective enforce, and a backend identity change mid-run aborts it. test: `tests/scripts/test_community_label_study.py::test_j_arm_enforces_with_alias_request_and_aborts_on_backend_change`.
- 3.3.6 - `score` emits the complete ship configuration for each branch: gate `off` (rule 1), gate `enforce` (rule 2), and D5 with `enabled: false` and every consumer `off`, including under `--no-decisions`. test: `tests/scripts/test_community_label_study.py::test_ship_configuration_follows_each_branch`.

### 3.4 Label-quality report and ship decision (depends: 3.2, 3.3) [category: docs]
`kind: deliverable`

Targets:
- `docs/evidence/community-labels-2026-09/label-quality.md`

Run order:

1. Josh gives his go for D4, and the Orchestrator re-registers C1 (Coordination).
2. Josh runs `prepare`.
3. The Assistant sends Josh the sheet.
4. Josh returns it rated, and the developer runs `score`.

The report records the sample, both corpora, every bar with its value and verdict, the
cost, and the ship decision with the exact `ai.decisions` and `code_index.community_label`
values Josh applies.

Every metric below uses 3.3's definitions, and an undefined metric fails its bar.

Gate bars (Q1.6). A positive is G total >= D total:

- J beats D by at least 0.3 mean accuracy and 0.5 mean findability;
- J regresses below D on at most 1 of 30 communities (5%);
- gate precision >= 0.85 and recall >= 0.60, with `gate_admitted` as the predicted
  positive;
- mean confidence separation of the recorded `label_confidence` (admitted minus rejected)
  >= 0.15;
- schema-rejection rate <= 10% from the outcome counts;
- decision cost <= $0.05.

Carry bars. A positive is a carried name rated accuracy 2 against its new members.
Precision >= 0.85 and recall >= 0.60.

Sufficiency bars. A positive is D total >= G total, and the prediction is position >=
`min_score`. Precision >= 0.85 and recall >= 0.60.

Ship rules, in order:

1. If G regresses at most 5% below D, the gate ships `off`: the gate is not earning its
   keep.
2. Otherwise, if J passes every gate bar, the gate ships `enforce` with the study's
   `evaluated_model` and `evaluated_backend`.
3. Otherwise, D ships: `code_index.community_label.enabled: false`, and the gate, carry
   and sufficiency all ship `off` (D5). This branch is terminal.

Only under rules 1 and 2, where labeling stays enabled, do carry and sufficiency each ship
`enforce` with the study's evaluated pair if they pass their own bars, and `off`
otherwise.

If 3.2's OpenRouter checklist has an unmet item, the study runs `prepare --no-decisions`.
J, carry and sufficiency have no data, so rule 2 is unreachable and every consumer ships
`off`. Rule 1 or rule 3 decides whether labeling stays enabled.

**Research context:**

- Q1.6's evidence sources move from live `code_communities` rows and log events to the
  study's recording storage and run record, because D4 forbids live writes. The fields
  are the same: source, confidence, model, signature and outcome counts.
- `docs/evidence/community-labels-2026-09/` holds the Phase-0 evidence files.
- Planned: the operator runs and Josh's rating, then a review of every bar against the
  `score` output.

**Acceptance:**

- 3.4.1 - The Q1.6 label-quality study ran with arms D, G, and J, or with D and G alone under `prepare --no-decisions` when 3.2's OpenRouter checklist has an unmet item, and `label-quality.md` records the metrics against every bar, the carry and sufficiency bars, and the ship decision the live config follows. behavior: "ship" in `docs/evidence/community-labels-2026-09/label-quality.md`.

### 3.5 Box consumer evidence (depends: 3.2, #24248) [category: docs]
`kind: deliverable`

Targets:
- `docs/evidence/decisions/wire-contracts.md`

After #24248 closes, append a "Decision shape consumers" section to the wire note. It
names one product consumer and one live-path test per shape:

- Choice: 2.1's `test_gate_admits_at_threshold_and_rejects_below`.
- Noul: 2.2's `test_carry_keeps_name_above_threshold_without_generation`.
- Score: 2.3's `test_sufficient_deterministic_label_skips_generation`.
- Box: #24248's
  `tests/mcp_proxy/tools/test_decisions_tools.py::test_evaluate_round_trips_every_decision_shape`,
  with #24248's closing commit and 3.2's OpenRouter Box capture result.

If #24248 closes without a Box round trip in that test, this leaf reports the gap to
Triage and stays open.

**Research context:**

- J3 requires every shape to have a real consumer and one live-path test.
- Box's consumer is #24248 (Decision Record 9), which is open and blocked by #24246 at
  draft time.
- Planned: read #24248's closing commit and test, then update the note.

**Acceptance:**

- 3.5.1 - The wire note names a product consumer and its live-path test for each of Choice, Noul, Score and Box, including #24248's Box round trip and commit. behavior: "test_evaluate_round_trips_every_decision_shape" in `docs/evidence/decisions/wire-contracts.md`.

## V2 End-to-end verification
`kind: verification`

These are completion gates for the implementation. Except for plan validation, none has
run yet. Each runs after its leaves land, and each must hold before the epic closes.

- Plan validation is the only planning-time check: `uv run gobby plans validate
  .gobby/plans/community-label-decisions.md -p /Users/josh/Projects/gobby` must pass on
  every revision.
- The named tests must pass with the isolated `DATABASE_URL` and `GOBBY_TEST_PROTECT=1`:
  `tests/ai/test_decisions_service.py`, `tests/code_index/test_community_labeler.py`,
  `tests/config/test_decisions_config.py`, `tests/config/test_runtime_loop_consumers.py`,
  the HTTP contract tests, and `tests/scripts/`. `uv run ruff check src/ scripts/`,
  `uv run ruff format --check src/ scripts/`, `uv run mypy src/` and the test-types
  audit for touched tests must then pass too.
- After P2 lands and the Orchestrator restarts the daemon, a maintenance pass with every
  consumer `off` must log 6.2's outcomes unchanged.
- P3 must not close before Josh's runs: both captures, the study, his rating and the ship
  configuration he applies.
- The full suite must not be run.

## V1 Plan Changelog
`kind: verification`

- 2026-10-24, Plan Writer gobby#17508, first draft for #22604. Encodes the Orchestrator
  rulings D1, D2, D3, D4 and D5, Josh's rulings J1, J2, J3 and D6, the strict Score
  profile, and the Choice and Noul relabel. Box stays isolated in 1.3 pending D7 (Box
  consumer and backend, routed to Josh with A recommended for both). The study and capture
  pin moved from `typesafe/jev-1.13` to `typesafe/jev-1.14`, because Box requires 1.14 and
  one pin keeps one evaluated identity.
- 2026-10-24, Plan Writer gobby#17508, revision for Plan Adversary findings PA-001 to
  PA-007 and the advisor review:
  - Encoded D7a = A and D7b = A (Josh), and the Orchestrator's approval of the 1.14 pin
    and the separate transports.
  - The J arm's `evaluated_model` is now the request model, with a regression test.
  - Per-consumer `off` semantics, with three new tests.
  - Total metric definitions, D5 as a terminal ship branch, and a `--no-decisions` study
    path for a failed OpenRouter capture.
  - A fresh service per capture shape, with Box last.
  - The #24701 `blocked-by` ordering.
  - New leaf 3.5, which waits on #24248 for Box's consumer evidence.
  - Removed the overclaimed contract clause from 2.2.3 and 2.3.3, and added the
    lifecycle test consumers to 2.1's unchanged inventory.
- 2026-10-24, Plan Writer gobby#17508, revision for Plan Adversary findings PA-008 to
  PA-010:
  - PA-008, under the Orchestrator's ruling: confidence separation again measures the
    recorded Choice `label_confidence` over admitted and rejected communities, as Q1.6
    and #22604 require.
  - PA-009: 3.3 now carries the exact bars and ship rules, including the evaluated pair
    and `--no-decisions`.
  - PA-010: the carry arm reuses the 30 main communities as seeded, perturbed copies,
    and each copy rates the pre-perturbation G name against the new members.
  - 3.4.1 now names the `--no-decisions` fallback.
  - V2 is now written as completion gates.
- 2026-10-24, consensus. Plan Writer gobby#17508 and Plan Adversary gobby#17509 agree on
  this plan as of d7553d7a44. The adversary re-reviewed those bytes, confirmed that the
  live plan equals the commit, and reran project-aware base validation, which passed.
  Findings PA-001 to PA-010 are all resolved, and no blocking finding or governing
  decision remains open. The adversary accepted the carry arm with no population floor,
  because the run record counts skips and a zero denominator fails its bar.
