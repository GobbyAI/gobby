# #24215 working context: LM Studio as a decision backend

Plan: `.gobby/plans/decision-classifier-path.md` (2,560 lines; M1 at line 2168).
Writer gobby#16490 drafts; Adversary gobby#16491 reaches consensus; Orchestrator
gobby#15977 reviews; Josh decides replace vs join through the Assistant.
Not registered in gobby-plans (not in the active list); amend before expansion.

## Evidence (2026-10-12)

Primary, local LM Studio server at `127.0.0.1:1234` (Josh's machine):
- `GET /v1/models` cards carry only `id`, `object`, `owned_by`, so the
  Decision 10 `model_card` identity fields are absent.
- `GET /api/v1/models` (native) lists `type` `llm` or `embedding` per model,
  with key, quantization, format, size, variants and `loaded_instances`. No
  `decision`-type model is downloaded. The only loaded instance is
  `text-embedding-nomic-embed-text-v1.5@f16`, which was already loaded.
- Validation probes, sent as POSTs before 15977 restricted probing to GET.
  No real model was named and nothing was loaded:
  - `POST /v1/systemone {}`: `request.state: Invalid input`,
    `request.questions: Required`. There is no `model` error, so `model` looks
    optional.
  - noul without instructions: `questions.q1.instructions: Invalid input`.
  - choice without criteria: `questions.q1.criteria: Required`.
  - complete noul, no model: `noModelsLoaded` ("No models loaded. Please
    load a model in the developer page or use the 'lms load' command.").
  - `POST /v1/decisions {}`: `model` Required, `input` Invalid,
    `questions` Required. Questions must be an array, and each item requires
    `instructions` and `choices`. This is a different shape from the plan's
    `{model, state, questions{}}` wire.
- A later unauthenticated `GET /v1/models` and `GET /api/v1/models` both
  returned 200, so server authentication was off. An earlier plan recorded
  a 401 without a token on `/api/v1/models`
  (`.gobby/plans/completed/task-15606-ui-cleanup.md:281`).
- So LM Studio's `/v1/systemone` accepts the plan's systemone question shape
  (`type`, `instructions`, `criteria`), and the existing `systemone` wire
  needs no change. Response shape, answer types, `usage.input_tokens`, the
  `model` echo or resolution, the input limit and truncation behavior are
  all UNVERIFIED, because no decision model is installed.

Doc snippets, from WebSearch restricted to lmstudio.ai. WebFetch is
egress-blocked, so these are cited as search snippets, retrieved 2026-10-12:
- LM Studio 0.4.26 (changelog dated 2026-10-08) adds `/v1/systemone`
  ("Jev (TypeSafe AI) compatible") and `/v1/decisions` ("OpenAI compatible").
  Bionic 1.1.8 requires LM Studio 0.4.26 or newer.
  URLs: https://lmstudio.ai/changelog/lmstudio/lmstudio-v0.4.26 and
  https://lmstudio.ai/changelog/bionic-v1.1.8
- https://lmstudio.ai/docs/developer/jev-compat/systemone: POST; use a
  loaded decision model's identifier; body is `state` plus `questions` keyed
  by ID; types are noul, choice (criteria object) and score (ordered
  criteria array); the response returns `answers` keyed by question ID.
- https://lmstudio.ai/docs/developer/openai-compat/decisions: POST, needs a
  loaded decision model.
- https://lmstudio.ai/docs/developer/rest/load: `POST /api/v1/models/load`
  accepts decision models (type `llm`, `embedding` or `decision`).
- No snippet names which decision models LM Studio serves (Clef? Kev? Jev
  clones?). That is unverified.

Memories:
- c0ba0b7c: Gobby must not load, unload, download, delete or otherwise manage
  LM Studio models, and it uses the native `/api/v1` for LM Studio-specific
  operations.
- ecb8f392: LM Studio is a serial resource.
- 606b6838: Gobby does not manage local-generation model lifecycles.
- a5ad8b5c, cited in Decision 3, no longer exists ("Memory a5ad8b5c not
  found").

Other plans:
- `local-inference-runtime-foundation.md` (lines 40-68) says "Gobby manages
  selected model lifecycle" and lease-driven load and unload for LM Studio
  roles. That conflicts with this plan's Decision 3 and memory c0ba0b7c, so
  the decide role stays out of that plan.
- The Graphify parity plan references only #22604's held Jev choice gate and
  makes no backend choice, so it is unaffected.

## Direction (written into the plan as Decision 17 and its dependents)

- "Constraint 3" in the task is Decision 3's bullet at line 102-103: "out of
  scope because its family roles cover LM Studio, Ollama and vLLM, and Kev
  supports none of them." Rewrite it: LM Studio 0.4.26 now serves
  `/v1/systemone`, so it is a third operator-run `/v1/systemone` server
  beside Kev and `clef_mlx.py serve`, on the existing `systemone` wire with
  no new wire and no code change. The local-inference plan stays out of
  scope, because its role layer manages model lifecycle and Decision 3 and
  c0ba0b7c forbid that for decisions.
- `/v1/decisions` (OpenAI-compatible) is not adopted (restraint rung 1): its
  shape differs, and `/v1/systemone` already serves the wire.
- Identity: the LM Studio `/v1/models` card has no Decision 10 fields, so
  `model_card` yields no identity. Whether `response.model` is echoed or
  resolved is unverified, and the live capture decides. Until then LM Studio
  is a shadow and evaluation backend like local Clef (a new Decision 17;
  Josh-approved Decision 16 stays unchanged).
- Decision 8: `backend_max_state_tokens` for LM Studio is unknown; the
  operator sets it from the live capture, and the Activation Gate item 4
  `model_card` branch needs a server source commit, which LM Studio, a closed
  app, cannot supply.
- Decision 3 operator duties: the operator loads the decision model (`lms
  load`) and should disable JIT loading or set the identifier, so a decision
  request never loads a model on Gobby's behalf.
- Replace vs join: recommend join (zero code difference; same wire), and
  name it for Josh as a decision. Restraint favors LM Studio as the
  recommended local server, since it is already installed and running and
  avoids `clef_mlx.py`'s single-request lock and unauthenticated
  remote-image fetch, but which decision models it serves is unverified.
- Sections to touch: Overview (endpoint list), Decision 1 (systemone
  servers), Decision 3, Decision 8 (limit default note), Decision 10
  (identity note), Decision 16 (extend to LM Studio), Activation Gate item 2
  (server list), 4.1 docs rows if they list local servers (line ~1975), V1
  entry, and M1 whole withdrawal for re-derivation.
- Check acceptance items mentioning `clef_mlx.py` (lines 1039, 1164, 1269,
  1975) to see whether LM Studio needs parallel wording.
