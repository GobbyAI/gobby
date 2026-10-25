# Working context: plan for #22604 (community-label decisions)

Session-scoped working note for the Plan Writer gobby#17508. Current state only.

## Assignment and seats

- Task #22604 "Gate generated names through a Jev `choice` decision" (parent #24238, open,
  unblocked, backend-developer, tdd). Read its full description: it has three layers, the
  stamped 6.3 text, the "Consumes the decision-classifier-path plan" section (authoritative
  over 6.3), and Josh's 2026-10-24 ruling plus widening ("So we can test noul, multiple
  choice, etc.": one live-path test per shape, each asserting the decision drives product
  behavior).
- Plan path: `.gobby/plans/community-label-decisions.md`, Plan ID `community-label-decisions`.
- Coordinator/Triage: b966e398-fee9-4efc-b338-fdaf08e3c839 (gobby#17480). Report there.
- Plan Adversary: b91baf24-aa46-4d63-9f2b-b3f9e20df842 (gobby#17509), waiting for path + SHA.
- No enhancer named in the brief.
- ORCH rulings (2026-10-24): D1 = A, D2 = A (below). J1 to J3 go to Josh through the
  Assistant. Draft them as provisional option A and continue to adversary consensus.

## ORCH rulings to encode

- D1 = A: both decisions land in Python now. The plan states, as a hard dependency, that
  #24701 (code-index-family 8.3 "Symbol summaries and community labels",
  `.gobby/plans/code-index-family.md` around lines 4288-4560) must preserve both decisions
  in its native job (hub decisions route or a Rust client) and must not delete them
  silently. 8.3 currently deletes `src/gobby/code_index/community_labeler.py`,
  `summarizer.py`, `community_label_safety.py` and `_storage/communities.py`, and writes
  `label_confidence` NULL. ORCH amends 8.3 before #24701 dispatches.
- D2 = A: this plan builds `DecisionService.noul`, the three Noul fixtures, the
  `docs/evidence/decisions/wire-contracts.md` note, and the test
  `tests/ai/test_decisions_service.py::test_noul_returns_probabilities_by_key`. At expansion
  those come off #24246 (3.1.1), its anchor is substituted, and a blocked-by edge goes from
  #24246 to this plan's noul leaf. Name these edits in a Coordination section. This replaces
  decision-classifier-path's "no ordering edge between #22604 and 3.1" statement.
  Expansion still needs Josh's go.

## Final rulings received 2026-10-24 (supersede the J list below)

- J1 = A final, refined: Jev over OpenRouter (key already in Gobby secrets); captures
  operator-run. LM Studio 127.0.0.1:1234 shadow/eval, model id Josh-filled, default
  ggml-org/Clef-Flash-GGUF Clef-Flash-Q8_0.gguf (comparator Clef-Q8_0 27B); 64-question
  batches, Noul and Score unverified until captures. Kev and TypeSafe options removed.
- J2 = A final: Josh rates ~120 blinded items; sheet file sent by Assistant; score step
  reads the completed sheet.
- J3 = B final: build Score (score() + fixtures + wire note mirroring noul), real
  consumer, one live-path test per shape, captures add Score round trip per wire,
  coordination edits for decision-classifier-path Decision 6/7, 3.3 evaluate exposure,
  #24246.
- Sent to Triage (msg d84ab6a0): egress denials (docs.typesafe.ai, zuplo.com); Score
  wire schema research request; D3 Score consumer (rec A: deterministic-label
  sufficiency, `community_label_sufficiency: ScoreConsumerConfig(min_score)`, evidence from
  D/G ratings); D4 C1 registration (rec A: ORCH registers on operator GO); D5 ship gap
  (rec A: ship D, `code_index.community_label.enabled: false`).
- Isolated `gobby_gcode_test` DB does not exist; `gobby_test` has no code_communities.
- Study backend: OpenRouter `typesafe/jev-1.13`, response_version (only enforceable one).
- Cutover hold BEFORE 20:51 (7d5391e4d4): no cargo/pytest until DAEMON BACK.

## Josh decisions (superseded, kept for history)

- J1 keys/captures. Agents cannot read the secrets table, so keyed steps are operator-run.
  Josh stores an OpenRouter key with `gobby secrets set`. A: LM Studio at 127.0.0.1:1234 with
  a decision model loaded (shadow/eval backend only, Decision 17). B: Kev. C: TypeSafe key.
  Josh runs prepared commands: one capture per wire and the Q1.6 study run. Enabling a
  hosted endpoint in live config is Josh's action (Decision 2).
- J2 rater. A: Josh rates the blinded sheet (90 D/G/J items plus 30 carry items). B: a fresh
  agent seat from a different model family.
- J3 Score. A: no Score (Decision 7). B: build Score.

## Repo facts

- `src/gobby/ai/decisions.py` (420 lines): `ChoiceQuestion(instructions, criteria)`,
  `ChoiceAnswer(choice, probabilities, confidence)`, `DecisionResult[A](answers,
  backend_identity, response_model)`, `DecisionsUnavailable(reason, detail)`, reasons
  unconfigured/cooldown/oversize/invalid_request/truncated/timeout/transport/http_status/parse.
  `DecisionService.choose(consumer, state, questions, *, timeout_seconds=None)` at :117-163,
  `_dial` :165-233, `_check_request` :236-250 (1-64 questions, key pattern, 2-255 options),
  `_request_body` :253-270 (type "choice"), `_parse_answers` :324-336, `_parse_choice`
  :339-360, `_schema_hash` :414-416 (sorts criteria; must handle Noul), `get_decision_service`
  :88-94, `estimate_tokens` :83-85. `noul` does not exist.
- Noul wire facts: `docs/evidence/decisions/wire-contracts.md` lines 33-41. Question is
  exactly `{"type": "noul", "instructions": <non-empty>}`. Answer `type: "noul"` with the
  probability in field `noul`, finite in [0,1], and no confidence. Existing fixtures:
  `tests/ai/fixtures/{systemone,openrouter_decisions,workers_ai}_choice_response.json`.
- The #24246 spec for noul (adopt verbatim): `async noul(consumer, state, propositions:
  Mapping[str, str], *, timeout_seconds=None) -> DecisionResult[NoulAnswer]`,
  `NoulAnswer(probability: float)`, same transport/ceiling/cooldown/strict parsing. Fixtures
  `tests/ai/fixtures/systemone_noul_response.json`, `openrouter_decisions_noul_response.json`
  (dated model, id, provider, usage with output_tokens and cost), `workers_ai_noul_response.json`
  (REST envelope), three propositions each, authored before code. Append a fixture note to
  wire-contracts.md.
- `src/gobby/config/ai.py`: `DecisionConsumerMode` off/shadow/enforce;
  `_DecisionConsumerConfig` :290-317 (mode, evaluated_model, evaluated_backend; enforce
  requires both); `ChoiceConsumerConfig` :320-328 (min_confidence 0.5);
  `RerankConsumerConfig` :331-339 (min_probability 0.5); `DecisionsConfig` :365-452 with
  fields community_label, tool_rerank, found_work. A new consumer field means regenerating
  `crates/gcore/assets/config/runtime_config_contract.json` with
  `uv run python scripts/generate_runtime_config_contract.py`, plus
  `tests/contracts/http/config_schema.json` and `config_values.json`, the ai.decisions row in
  `docs/audits/configuration-audit.md` (line 290), and the CONSUMERS tuple in
  `tests/config/test_decisions_config.py:33`. Commit 635f7c8417 (#24243) shows the full set.
- `src/gobby/code_index/community_labeler.py` (219 lines): `CommunityLabelOutcome` :49-56 (5
  values); `GeneratedLabel(name, rationale, model)` :59-65; `CommunityLabeler.__init__(
  text_generation, config: CodeIndexConfig)` :71-77; `generate_batch` :79-84; `label_batch(
  project_id, communities, *, storage, run_db)` :86-132 logs
  `code_index.community_label.outcome` (debug, extra project_id, community_id,
  member_signature, outcome) and the info batch line; `_write_label` :160-188 returns None
  when the guarded write loses, and the caller `continue`s silently today (6.3 adds
  `write_superseded`). `label_model` today is `self._profile`.
- Candidates: `label_candidates` holds up to four distinct strings led by the deterministic
  label (gcode-import-communities 2.2.9). Choice options are `generated` plus
  `deterministic_<i>` for each candidate, so 2 to 5 options. Rule: `deterministic_<i>` writes
  `label_candidates[i]`, and `generated` below threshold writes `label_candidates[0]`.
- Carry-forward (gcode-import-communities 2.3, plan lines 540-557): a matched community with
  a changed signature and prior source `model` keeps label, labeled_signature (old),
  confidence and model, so it is "stale". It re-enters the queue because
  `labeled_signature != member_signature`, and readers show the deterministic label while
  stale. A deterministic prior has its gate fields reset to None. Stale-model predicate:
  `label_source == "model" and labeled_signature is not None and labeled_signature !=
  member_signature`.
- Storage: `src/gobby/code_index/_storage/communities.py::update_community_label` :36-74
  (signature-guarded UPDATE, sets labeled_at NOW, label_attempted_at NULL; returns bool),
  `mark_community_labels_attempted` :76-99, `get_unlabeled_communities` :18-34.
  `StoredCommunity` is in `src/gobby/code_index/models.py:282-334`.
- Wiring: `src/gobby/runner_startup_code_index.py::_start_code_index_tasks` constructs
  `CommunityLabeler(runner.text_generation_service, config.code_index)`. A decisions resolver
  can come from `runner.config_runtime.capture().snapshot.active.ai.decisions`, read per call
  (Decision 10). Maintenance: `src/gobby/code_index/maintenance.py::_label_unlabeled_communities`
  :419-441.
- Tests: `tests/code_index/test_community_labeler.py` has 6.2 tests at lines 160-338, among
  them `test_every_outcome_logs_at_debug_under_one_info_batch_summary` (the 6.2 five-outcome
  test, left untouched).
- Docs: `docs/guides/llm-features.md:172-177` holds the symbol_summary row and paragraph. No
  community_label row exists yet (6.3.3).
- Q1.6 (gcode-import-communities.md:1809-1825): 30 communities, 15 per corpus (Gobby plus the
  frozen Game Goblins baseline under `/Users/josh/Projects/wiki-bakeoff-code-2026-09/`),
  stratified by size; arms D/G/J; blind accuracy 0-2 and findability 0-2. Bars: J beats D by
  >= 0.3 accuracy and >= 0.5 findability; J regresses <= 5% below D; gate precision >= 0.85
  and recall >= 0.60; confidence separation >= 0.15; schema rejection <= 10%; cost <= $0.05.
  If G already regresses <= 5%, ship with the gate off. Game Goblins is not a daemon project
  (only `gobby` is registered). Q1.4 indexed both corpora in the isolated `gobby_gcode_test`
  DB (`docs/evidence/community-labels-2026-09/churn.md`).
- Activation Gate item 4: `.gobby/plans/decision-classifier-path.md:958-992`. Evidence goes
  in `docs/evidence/decisions/<wire_api>-live-<date>.md`, and it requires one Choice and one
  Noul round trip, so noul lands before the captures. Decision 10 enforce rule is at :325-390.

## Design to draft

- Phases: P1 Service and gate; P2 Live evidence and ship decision.
- 1.1 noul() on DecisionService plus fixtures plus wire note (D2). Mirrors 3.1.1.
- 1.2 Choice gate in CommunityLabeler (6.3.2, 6.3.4, 6.3.1 substituted). Reads
  `ai.decisions.community_label` per call through a resolver; batches of at most 10
  communities sized to `service.max_input_tokens` with at most 64 questions; keys
  `c<community_id>`. Enforce acts only when evaluated_model == model and evaluated_backend ==
  result.backend_identity; otherwise it runs as shadow. Shadow writes the 6.2 outcome
  unchanged and logs choice/confidence on the outcome event. `DecisionsUnavailable` maps to
  `transport_failed` (stamp only). `label_model` = `response_model`. `write_superseded` for
  every write path. Exactly nine discriminator values.
- 1.3 Noul carry consumer: new `DecisionsConfig.community_label_carry: RerankConsumerConfig`
  (reuse the class, docstring fits), config regen set above. A stale model row gets a Noul
  proposition "The name `<label>` describes what these files have in common." If
  p >= min_probability in enforce, re-stamp `labeled_signature` keeping label, source,
  confidence and model (skip generation); otherwise generate and gate. Carry outcomes: decide
  whether to add discriminator values (6.3.4 says exactly nine, so prefer a separate
  `carry` field on the event, or a separate event `code_index.community_label.carry`). Depends
  on 1.1 and 1.2.
- 1.4 Docs: llm-features.md row `code_index.community_label` plus the gate and carry
  paragraph (6.3.3).
- 2.1 Live captures (6.3.6 substituted): one per wire, operator-run; probably a capture
  script under `scripts/` that drives DecisionService and writes the item-4 evidence. Stub
  tests with a fake transport.
- 2.2 Q1.6 study plus carry sub-study plus ship decision (6.3.5): study script reads sampled
  communities from the isolated DB, runs label_batch with a recording storage for G (off) and
  J (enforce, evaluated_* = captured identity) over identical memoized generations, and emits
  a blinded sheet. The carry sub-study perturbs memberships for 30 labeled communities. A
  score step writes `docs/evidence/community-labels-2026-09/label-quality.md` with the ship
  decision and the live config values Josh applies.
- Coordination section: D1 hard dependency on #24701/8.3; D2 edits to #24246; #22604
  description substitutions already in place.
- Keep the stamped M1 of gcode-import-communities untouched. Never write `## M1`.

## Mechanics

- Load before drafting (after compaction): restraint, brevity, plan drafting.md, coverage.md,
  and the `docs/contracts/plan-coverage.md` grammar (not yet read in full).
- Validate: `uv run gobby plans validate .gobby/plans/community-label-decisions.md -p /Users/josh/Projects/gobby`.
- Flow: draft, validate, send to the adversary (path + SHA), iterate to consensus, add a dated
  `## V1 Plan Changelog` consensus entry, commit, send the SHA to the adversary (it derives
  M1), commit the rendered plan, send the stamped plan to Triage, then end_agent_run (standing
  seat: only when directed).

## Plan design v2 (write the plan from this; Triage: hold D3-D5 and Score-schema sections until ORCH rules, draft the rest)

Format: copy `.gobby/plans/completed/spawn-network-override.md` (Plan artifact line, `# Title`,
`**Plan ID:**`, `## Context` framing, `## P<N>:` framing phases, `### N.M Title (depends: ...) [category: code]`
+ `` `kind: deliverable` ``, `Targets:` block with no blank line, `**Research context:**`,
`**Acceptance:**` items `N.M.k - prose. test: path::symbol`). End with `## V2 End-to-end verification`
(`kind: verification`) and `## V1 Plan Changelog` (`kind: verification`). Never write M1.
Framing sections: Context, Decision Record, Coordination, Constraints. Expansion root expected: #22604
(becomes the epic; keeps its gcode-import-communities covers labels; 6.3 anchor map in prose:
6.3.1 substituted (#24244 test) plus sizing in 2.1; 6.3.2 -> 2.1 test_gate_admits_at_threshold_and_rejects_below;
6.3.3 -> 2.4; 6.3.4 -> 2.1 test_gate_outcomes_extend_the_discriminator_to_nine; 6.3.5 -> 3.4 "ship";
6.3.6 -> 3.2 captures).

Leaves (10):
- P1 Decision shapes
  - 1.1 `DecisionService.noul` [code]. Targets `src/gobby/ai/decisions.py::*` (scope-reason: add NoulAnswer,
    noul, estimate_request_tokens; generalize _check_request/_request_body/_parse_answers/_dial/_schema_hash),
    `tests/ai/test_decisions_service.py::*`, 3 new Noul fixtures (bare), `docs/evidence/decisions/wire-contracts.md`.
    Spec verbatim from decision-classifier-path 3.1 (lines ~1844-1862). Public `DecisionService.estimate_request_tokens(state, questions)`
    (Choice map or Noul proposition map) = the estimate choose/noul compare with max_input_tokens; consumers pack with it.
    Tests: test_noul_returns_probabilities_by_key (3.1.1 anchor verbatim), test_noul_rejects_malformed_answers_as_parse,
    test_estimate_request_tokens_matches_oversize_guard; behavior "systemone_noul_response.json" in wire-contracts.md.
  - 1.2 `DecisionService.score` [code] (depends: 1.1). Same files + 3 Score fixtures. FIRST pin Score request/answer
    schema in wire-contracts.md from the researcher's transcription (PENDING ORCH research). `ScoreAnswer(score, probabilities, confidence)`;
    levels 2-10 ordered low->high; normalize position to [0,1]. Tests test_score_returns_levels_by_key, malformed -> parse.
- P2 Community-label consumers (all in `src/gobby/code_index/community_labeler.py::*` + `tests/code_index/test_community_labeler.py::*`)
  - 2.1 Choice gate (depends: 1.1). Also `src/gobby/runner_startup_code_index.py::_start_code_index_tasks` (pass
    `decisions_resolver=lambda: runner.config_runtime.capture().snapshot.active.ai.decisions`). Consumers unchanged:
    `src/gobby/code_index/maintenance.py` (label_batch signature unchanged). `CommunityLabeler(..., *, decisions_resolver=None)`;
    None/off = 6.2. Gate only communities whose generated name sanitizes to a model label (candidate-equal stays written_deterministic).
    Questions keyed `c<community_id>`, instructions name the key, criteria `generated` (sanitized name) + `deterministic_<i>` per
    label_candidates (2-5 options). State `{"communities": {c<id>: {representatives, members (reps first, cap 40), candidates,
    generated: {name, rationale}}}}`; no prompt sanitization (answer can only name an offered option; parser validates).
    Pack greedily, <=64 questions, estimate_request_tokens <= config.max_input_tokens; sequential requests; singleton oversize ->
    DecisionsUnavailable -> transport_failed. Effective enforce iff mode enforce and evaluated_model == config.model and
    evaluated_backend == result.backend_identity; else shadow + warning `evaluated_backend_mismatch` once per failure_cooldown_seconds.
    Enforce: generated with confidence >= min_confidence -> gate_admitted (label sanitized, source model, label_confidence=confidence,
    label_model=response_model, labeled_signature); deterministic_<i> -> gate_rejected writes label_candidates[i] source deterministic
    with confidence+model+signature; generated below threshold -> gate_rejected writes label_deterministic. Unavailable -> transport_failed
    (attempt stamp only). Shadow: 6.2 writes unchanged; event gains decision_mode/decision_choice/decision_confidence/decision_unavailable.
    Every write path whose guarded update returns False -> write_superseded (no stamp; replaces today's silent continue).
    Exactly nine enum values. Tests (FakeWire-style httpx.MockTransport monkeypatch, real DecisionService):
    test_gate_admits_at_threshold_and_rejects_below (6.3.2), test_gate_outcomes_extend_the_discriminator_to_nine (6.3.4),
    test_gate_batches_fit_the_request_ceiling, test_enforce_without_matching_evaluation_runs_as_shadow (also mode change next call),
    startup wiring test (find existing runner startup test file first).
  - 2.2 Noul carry check (depends: 2.1). Config: `src/gobby/config/ai.py::*` add `DecisionsConfig.community_label_carry:
    RerankConsumerConfig`; carriers `crates/gcore/assets/config/runtime_config_contract.json::*` (regen via
    `uv run python scripts/generate_runtime_config_contract.py`), `tests/contracts/http/config_schema.json::*`,
    `tests/contracts/http/config_values.json::*`, `docs/audits/configuration-audit.md` (ai.decisions row note),
    `tests/config/test_decisions_config.py::*` (CONSUMERS tuple :33, ["off"] * 3 at :100). Stale-model predicate:
    label_source=="model" and labeled_signature not None and != member_signature. Proposition "The name `<label>` describes what
    the files in community c<id> have in common." State current members. Re-sanitize old label against new members first (fail ->
    regenerate). Enforce p >= min_probability -> update_community_label(label, "model", label_confidence, label_model unchanged,
    labeled_signature=member_signature), skip generation. Separate event `code_index.community_label.carry` with outcome
    kept|regenerated|superseded|unavailable, mode, probability; outside the nine. Batch info line adds carried count.
    Tests: test_carry_keeps_name_above_threshold_without_generation (Noul live-path), test_carry_below_threshold_or_shadow_regenerates.
  - 2.3 Score sufficiency check (depends: 1.2, 2.2) PENDING D3. `ScoreConsumerConfig(min_score)` + `community_label_sufficiency`;
    4 levels misleading/vague/clear/clear and specific; enforce score >= min_score writes deterministic label, skips generation;
    event `code_index.community_label.sufficiency`. Test test_sufficient_deterministic_label_skips_generation.
  - 2.4 Docs [docs] (depends: 2.3): `docs/guides/llm-features.md` row `code_index.community_label` + paragraph (gate, carry,
    sufficiency; no AICapability; not a profile route). decision-classifier-path 4.1 also edits this file later (different rows).
- P3 Live evidence and ship decision
  - 3.1 Capture script [code] (depends: 1.2): `scripts/decisions_live_capture.py`, `tests/scripts/test_decisions_live_capture.py`
    (tests/scripts has __init__.py). Flags --wire-api --api-base --model --identity-contract --backend-max-state-tokens
    --api-key-secret NAME (resolve via `src/gobby/storage/secrets.py::SecretStore.get` under operator DB role) --alias --out.
    Builds its own DecisionsConfig (never live config). Runs Choice, Noul, Score round trips via DecisionService; raw httpx probes
    for chars/token ratio, just-under and over backend_max_state_tokens, alias check, records usage.cost; OpenRouter: canonical_slug
    from GET https://openrouter.ai/api/v1/models?output_modalities=decisions and zdr/data_collection flags. Writes Markdown, no key.
  - 3.2 Captures evidence [docs] (depends: 3.1): `docs/evidence/decisions/openrouter-decisions-live.md`,
    `docs/evidence/decisions/systemone-live.md` (date inside; re-capture appends dated section). Josh runs prepared commands
    (developer requests through Triage/Assistant). Item-4 checklist (decision-classifier-path :958-992). LM Studio item 4 failing
    leaves it shadow/off and does not block the ship decision. Update wire-contracts.md unverified table.
  - 3.3 Study tooling [code] (depends: 2.3, 3.1): `scripts/community_label_study.py` + tests. `prepare`: read code_communities
    read-only for gobby + C1 (D4 pending), eligible member_count>=3, stratify size terciles, 5 per tercile per corpus, seed 22604;
    G and J arms via label_batch with in-memory recording storage and memoizing text-generation wrapper; J on OpenRouter
    typesafe/jev-1.13, evaluated_* from a probe decision's identity; abort if any J batch ran as shadow; carry: 30 extra
    communities (15/corpus) with model-label G names, perturb (odd mild: drop 10% non-representative members; even heavy: replace
    half with members of the next sampled community), run carry enforce; sufficiency over the main 30 deterministic labels.
    Blinded sheet CSV (item_id, members, name, accuracy 0-2, findability 0-2), dedupe identical names per community; key file
    kept separate. `score`: read completed sheet, compute every bar, write label-quality.md section.
    Cost = sum estimated_tokens x (4 / measured chars-per-token) x (usage.cost / input_tokens from the capture).
  - 3.4 Study report and ship decision [docs] (depends: 3.2, 3.3): `docs/evidence/community-labels-2026-09/label-quality.md`.
    Bars (Q1.6 :1809-1825): positive = G total >= D total; J-D >= 0.3 accuracy, >= 0.5 findability; J regress <= 5% (<= 1 of 30);
    precision >= 0.85, recall >= 0.60; confidence separation >= 0.15 (answer.confidence admitted minus gate_rejected); schema
    rejection <= 10%; decision cost <= $0.05. Ship: G regress <= 5% -> mode off; else J passes all -> enforce with study
    evaluated_model/backend; else D5 (pending, rec ship D). Carry bars: positive = accuracy 2; precision >= 0.85, recall >= 0.60 ->
    carry enforce else off. Sufficiency bars: positive = D total >= G total; same precision/recall. Live config applied by Josh.
    Study run (operator) + Josh rating (Assistant sends sheet).

Coordination text: D1 constraint on #24701 / code-index-family 8.3 (preserve gate, carry, sufficiency in native job; no silent
delete; ORCH amends 8.3 before dispatch). D2: at expansion remove noul()/fixtures/note/3.1.1 test from #24246, substitute its
3.1.1 anchor with 1.1.1, add #24246 blocked-by 1.1. J3: decision-classifier-path Decision 7 superseded (Score built here);
Decision 6 and 3.3 evaluate exposure gain Score; #24246 unchanged otherwise. Replace "no ordering edge between #22604 and 3.1".
Do not edit gcode-import-communities M1. Restart after landing P2 announced with global send_message (ORCH runs it).
Constraints: GOBBY_TEST_PROTECT + isolated DATABASE_URL; no full suite; agents never read secrets; operator-run commands;
monolith ceiling (community_labeler.py 219 lines, decisions.py 426 lines: fine).
Granularity decision: tooling split from operator evidence (3.1/3.2, 3.3/3.4) because evidence needs Josh's run.

## Rulings received 2026-10-24, second round

- D3 = A (ORCH): Score consumer is deterministic-label sufficiency.
- D4 = A (ORCH): re-register and re-index the existing C1 project id
  `c8e071af-e1f7-434f-b3c0-f31a1b1299c9` (`gcode-bakeoff-C1`, marker created 2026-09-19 at
  `/Users/josh/Projects/wiki-bakeoff-code-2026-09/corpora/gcode/C1/.gobby/project.json`).
  Operator-gated: Josh's explicit go at study time, before any registration. The study reads
  rows read-only and writes no live labels. Cleanup soft-deletes the registration after the
  study (`gobby projects delete`); never remove the folder. Observed 2026-10-24:
  `gobby projects list` shows only `gobby`.
- D5 = A (ORCH): G regresses >5% and J fails a bar ships D with
  `code_index.community_label.enabled: false`.
- Score profile (ORCH): strict Gobby profile. All five answer fields required; key set
  "0".."n-1" in request order; ranges; probability sum within tolerance;
  score = sum(i*p_i) within tolerance; 2-10 levels; fail closed as `parse`; never fill gaps.
  OpenRouter's optional fields fail the profile; record that as a capture finding.
- Choice/Noul (ORCH): relabel wire-contracts.md's narrower rules (instructions required,
  string-only descriptions, Noul exactly type+instructions, 2-option minimum) as Gobby policy
  over current upstream.
- D6 = B (Josh, via ORCH): build Box in #22604 with its own consumer, fixtures, wire note,
  evaluate exposure and a capture per wire. D7 (Box consumer and backend) sent to Triage
  (msg 4dde06cc) with A recommended for both halves.

## Score wire schema (researcher gobby#17515, message 5b02c76a, retrieved 2026-10-24)

Public documentation and schema evidence, no live backend verification.

- Semantics: Score is an ordinal expected index, not a probability of correctness. With n
  levels the index keys are the strings "0" to String(n-1), in request-array order.
- TypeSafe: `https://docs.typesafe.ai/api.md` failed retrieval. Read instead:
  `https://docs.typesafe.ai/api/system-one` and `https://api.typesafe.ai/openapi.json`
  (info.version "0.2.0").
  - ScoreQuestion: `type` const "score"; `instructions` string|object|array|null, optional;
    `criteria` array of string|object|array, required, minItems 1, maxItems 10.
  - ScoreAnswer: `type` const "score", `score` number, `confidence` number, `legend`
    object<string, string|object|array>, `probabilities` object<string, number>. All five
    required. Descriptions give confidence and probabilities in [0,1] with probabilities
    summing to about 1; the schema encodes no numeric bounds.
  - Prose `https://docs.typesafe.ai/primitives/score`: 2-10 levels, lowest first; positions
    start at zero; score = sum(i*p_i), range [0, n-1]; legend and probabilities use string
    index keys; confidence summarizes concentration. `https://docs.typesafe.ai/primitives/advanced`
    admits null entries that the OpenAPI excludes. Prose and schema disagree on the level
    minimum (2 versus 1).
- OpenRouter: `https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request`
  and `https://openrouter.ai/openapi.json` (DecisionsScoreQuestion/Answer).
  - Question: `type` "score", `instructions` string|object|array, `criteria`
    array<string|object|array>, all required; minItems 1, no maxItems.
  - Answer requires only `type` and `score` (double). `legend`, `probabilities` and
    `confidence` are optional. Documented example: indices "0","1","2", probabilities
    0, .01, .99, score 1.99.
  - SDK: `https://github.com/OpenRouterTeam/typescript-sdk/blob/main/src/models/decisionsscoreanswer.ts`
    and `decisionsscorequestion.ts`.
  - Response: `model`, `answers` and `usage` required; usage `input_tokens` and
    `output_tokens` required integers, `cost` optional; `id` and `provider` optional.
- Cloudflare Clef: `https://developers.cloudflare.com/workers-ai/models/clef/`; schema
  `https://github.com/cloudflare/cloudflare-docs/blob/production/src/content/workers-ai-models/clef.json`.
  Question requires `type` "score", `instructions`, `criteria` array minItems 2, maxItems 10,
  lowest first. Answer requires `type`, `score`, `legend`, `probabilities` (each in [0,1])
  and `confidence` in [0,1].
- llama.cpp / LM Studio: `https://lmstudio.ai/docs/developer/jev-compat/systemone`
  delegates to `https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1systemone-typesafe-compatible-system-one-api`.
  Question `type` "score", `instructions`, `criteria` 2-10 lowest first. Response example
  carries `type`, `score`, `legend`, `probabilities`, `confidence` in [0,1]; envelope `model`,
  `answers`, `usage` with `input_tokens` and `output_tokens: 0`. Upstream documentation only,
  not proof of the installed runtime.
- Choice and Noul against current upstream: TypeSafe OpenAPI makes `instructions` optional
  on every type, lets Choice descriptions be string|object|array|null with 1-255 options,
  and lets Noul carry optional `criteria.true`/`criteria.false`. OpenRouter requires
  `instructions` on Choice and Noul, and both `true` and `false` when Noul `criteria` is
  given. Choice answer `probabilities` and `confidence` are optional on OpenRouter and
  required on TypeSafe, Cloudflare and llama.cpp. Gobby's record is a narrower request
  profile; 1.1 relabels it as Gobby policy.

## Box wire schema (researcher gobby#17515, message 490d43e6, retrieved 2026-10-24)

- Semantics: Box asks where a described target is in an image. The answer is one
  rectangle: `left`/`right` are fractions of image width and `top`/`bottom` fractions of
  height, from the upper-left; smallest enclosing rectangle; edges are expectations.
  `confidence` is certainty of the box. No probabilities, legend, score or sum rule.
- TypeSafe OpenAPI 0.2.0 (`https://api.typesafe.ai/openapi.json`, BoxQuestion, BoxAnswer,
  BoxCoordinates, Asset, SystemOneRequest):
  - BoxQuestion: `type` const "box" required; `instructions` string|object|array|null
    optional; no `criteria`.
  - BoxAnswer: `type`, `box` and `confidence` required; `box` has `top`, `bottom`, `left`,
    `right` numbers; `confidence` number|null. No bounds or ordering encoded.
  - Request: optional top-level `assets` map<string, Asset>, Asset with optional `type`
    "image", `format` png|jpg|jpeg, `data_base64`, `part`. `POST /v1/systemone` requires the
    header `TypeSafe-Danger-Zone-Images: true` for images or Box.
- TypeSafe prose (`https://docs.typesafe.ai/primitives/box`, `https://docs.typesafe.ai/images`):
  one image required; edges in [0,1], left < right, top < bottom. Images and Box are
  experimental. Box requires Jev 1.14; the typesafe/jev-1.13 pin cannot serve it. Assets
  are referenced from state, instructions or criteria; every asset must be referenced.
  Limits: PNG/JPEG stills, file at most 16 MiB, each dimension 280-1568 px, area
  78,400-2,458,624 px, body at most 32 MiB, each reference at most 1,024 UTF-8 bytes. The
  exact reference syntax was not transcribed.
- OpenRouter (`https://openrouter.ai/openapi.json` DecisionsBoxQuestion/Answer; SDK
  `decisionsboxquestion.ts`, `decisionsboxanswer.ts`, `decisionsrequest.ts`): question
  requires `type` "box" and `instructions`; answer requires `type` and `box` (four doubles),
  `confidence` optional. DecisionsRequest declares no assets, images or files field. Image
  transport and Jev 1.14 availability are unverified.
- Cloudflare Clef (`clef.json`): Box is absent from both question and answer unions.
  Images exist separately (`images` array, max 4).
- llama.cpp / LM Studio: only choice, score and noul are documented for `/v1/systemone`.
  Images use `files` data URLs and need a vision model and projector.
- Proposed Gobby policy (researcher): four finite edges in [0,1], left < right,
  top < bottom; confidence finite in [0,1] when numeric; explicit null or missing handling
  per backend; never a sum check, never invented confidence.
