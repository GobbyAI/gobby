# Classification for Gobby: Jev, the TypeSafe skill, open clones, and the path to integration (2026-09-24)

**Status: research only.** Nothing was installed, downloaded, signed up for or loaded. No edits, and no task changes.

**Request.** Josh, by Telegram around 12:0x and 12:1x CT, relayed by #14069. Verbatim:

> "https://www.ayautomate.com/jev-builds — We may need to plan integrating classification sooner rather than later. I think Claude has a TypeSafe AI skill. Have the researcher look at it and do some research around other uses, and what it would take to integrate."

Second link: https://www.ayautomate.com/blog/jev-typesafe-system-one-model.

**This report updates earlier work; read that first.**
- **`docs/research/jev.md`** (#22623, closed 2026-09-20). It already covers Jev's API, a ranked list of Gobby use cases, the poor fits, three community MCP servers, a proposed native design, and the evaluation method. This report doesn't repeat any of that. It adds what has changed since 09-20 and answers Josh's specific questions.
- **#22604, "Gate generated names through a Jev `choice` decision"**: open, unclaimed, no parent, not blocked. It is plan section 6.3 of the gcode communities plan. It specifies a small `DecisionsClient` in `src/gobby/llm/decisions.py` that has never been written.
  - The config keys for it are already live: `code_index.community_label.decisions_*` at `src/gobby/config/code_index.py:58-79`, added by #22602.
  - Nothing reads them yet. The labeler writes `label_confidence=None` (`community_labeler.py:175`).

Web sources were treated as untrusted data and all were accessed on 2026-09-24. Jev launched on 2026-09-15, after the model's training cutoff. Numbers marked *(vendor)* come from TypeSafe and numbers marked *(builder)* from whoever built the example; nobody here measured them.

## Answers

1. **The linked pages.** Both are from AY Automate, an AI-automation agency. One is an index of community builds on Jev; the other is a blog explainer dated 2026-09-19.
   - Jev is TypeSafe's "System One" model. It returns a choice, a yes/no probability or a score, and never writes text.
   - The pages list its best uses as routing and triage, guardrails, reranking and scoring.
   - The agency says it hasn't used Jev on client work yet. §1 has the builds relevant to Gobby.
2. **The "TypeSafe AI skill" exists, but it comes from TypeSafe, not Anthropic.** It's `typesafe-ai` (github.com/typesafe-ai/skills, MIT, markdown only), published as a Claude Code plugin and through `npx skills` for Codex and Cursor.
   - It teaches a coding agent how to write calls to Jev's API. It classifies nothing itself.
   - Anthropic's skills repo has no classification skill. Claude's own route to classification is structured outputs, which Gobby already uses: Claude JSON calls pass a strict schema (memory c176f333).
3. **Uses in Gobby.** `jev.md`'s ranking holds up against today's code:
   - the memory-relevance shadow judge is the pilot;
   - skill and MCP-tool reranking is the likely product win;
   - found-work confirmation is the hot-path replacement.

   Josh's examples mostly fall outside that list. Telegram routing is deterministic today, and a classifier there adds risk without saving work. Sorting hook events also scores poorly (§3). Feedback triage and task labeling are real but low-volume, and the LLMs they use today are adequate. #22604's community-label gate is a sixth use, and it is already specified.
4. **What integration takes.**
   - **Where it sits:** its own small client next to generation and embeddings. It can't be a text-generation endpoint, because Jev uses its own wire protocol.
   - **Size:** about one small module, plus config and tests for each consumer.
   - **Token cost:** negligible, about $0.042 per million input tokens.
   - **The real costs:** the engineering, a latency budget on hook paths, and the privacy decision.
   - **What changed since 09-20:** there are now **open-weight, API-compatible clones**. The leader is Kev (Apache-2.0), which runs on MLX on Apple Silicon. That gives a local-first route that fits Josh's "provider-agnostic, MLX on Mac" direction for the golden-path installer (memory 5d9cd410).
5. **Recommendation.** When classification is taken up, start with #22604. It's already specified and bounded, but it is parked as an enhancement (§5). Make its `DecisionsClient`'s endpoint URL and model id configurable, so the same code reaches TypeSafe direct, OpenRouter and a local Kev server. Move the config to a shared `ai.decisions.*` block only when a second consumer (the relevance judge) passes its shadow evaluation. Details and decisions are in §4 and §5.

## 1. What the linked pages show

**AY Automate "Jev builds" [S1].**
- It's an index the agency says covers about 1,305 public builds from GitHub, X and LinkedIn.
- It gives $0.042 per million input tokens with output free, and 250–700 ms latency across the examples.
- It says the agency hasn't used Jev on client work yet.

The builds that matter for Gobby. All numbers are builder-reported:

| Build | What it does | Reported numbers | Gobby counterpart |
|---|---|---|---|
| PR review with 14 typed checks | One call asks 14 yes/no (Noul) questions: hardcoded secrets, SQL injection, auth changes and others | $0.00007 per PR, against about $14.50 per 1,000 PRs on Opus 5 | An advisory pre-screen before a close review. Never the close authority (`jev.md` "Poor fits"). |
| Fraud email triage with confidence routing | Low-confidence cases go to a larger LLM | 96/100 correct; about $0.07 and 16 s for the pipeline | The confidence-gated escalation pattern: found-work confirmation, falling back to the current LLM |
| JevRerank (LlamaIndex) | Reranks the top 10 retrieval hits down to 5 | nDCG@5 went from 0.340 to 0.396, at about $0.0003 per query | Tool, skill and memory reranking after hybrid search |
| GitHub Next's local Jev stand-in | Local replacement benchmarked across models | "pretty good on local machine (m5 max 64gb)" | Local route (§2.3) |
| Kev-0.5B (Jared Palmer) | Open Jev-style model | Runs on a MacBook Pro | Local route (§2.3) |

**AY Automate blog, by Boulanouar Walid, 2026-09-19 [S2].** It repeats TypeSafe's framing, with $40M in seed funding and ex-OpenAI founders.
- Latency: "70 to 500 milliseconds" *(vendor)*, and a 0.33 s median measured through OpenRouter *(third party)*.
- Access routes it names: TypeSafe direct (waitlist) and OpenRouter (added 2026-09-18).
- It says not to use Jev for writing, long reasoning, open-ended tasks or reading images.
- It mentions a developer who used Jev inside Claude Code "to address context window bloat from repeated tool calls".
- It doesn't mention a Claude skill, open weights or data retention.

## 2. What's new since `jev.md` (2026-09-20)

### 2.1 The "TypeSafe AI" skill

- **What it is:** `typesafe-ai`, in github.com/typesafe-ai/skills. MIT license, 2 commits, about 2.1k stars. The repo has only markdown and plugin metadata, with no scripts [S3][S4].
- **Who made it:** TypeSafe. It is not in Anthropic's skills repo (github.com/anthropics/skills), which has no classification skill [S5].
- **How it's installed:** in Claude Code with `claude plugin marketplace add typesafe-ai/skills` and then `claude plugin install typesafe@typesafe-ai`. In other agents with `npx skills add typesafe-ai/skills --skill typesafe-ai` [S4]. Nothing was installed for this report.
- **What its SKILL.md teaches.** Sections: Build with TypeSafe; Read the live docs; Find the useful shape; Design the judgments; Compose and verify.
  - Use Choice for one answer from a set, Noul for yes/no, and Score for a degree on a scale.
  - Ask independent questions in parallel in one call.
  - Treat probabilities as guidance, with thresholds validated on your own data.
  - Keep rules and exact calculations in code.
- **TypeSafe's stated reason for it:** agents trained on LLM APIs "ask one question per call and invent request fields" [S4].
- **Value to Gobby:** only as a reference for whichever agent implements #22604 or the native client. You could import it into gobby-skills at that point. It adds nothing to Gobby at runtime.

### 2.2 Access and privacy changes

- **Direct signup** at TypeSafe opened on 2026-09-20 and was **paused on 09-22**. Existing accounts keep working [S6].
- **OpenRouter** added Jev on 09-18 through its alpha Decisions API (`POST /api/alpha/decisions`); see `jev.md` [S2].
- **Vercel AI Gateway** also serves it, as `typesafe-ai/jev`, at the same price. One report says it's free until 2026-09-25 [S6][S7].
- **Model ids** on the direct API: `jev-latest` and `jev-1.13.0`.
- **Context limit, where sources disagree:** 64k shared per request, with 32k for state plus the longest question [S8]; Vercel lists 32k [S7]. `jev.md` records the same split.
- **Zero data retention is still unresolved.**
  - TypeSafe's legal page promises not to train on user data, and offers zero retention to "enterprise customers" through sales, with details in its DPA [S9].
  - A secondary source says Vercel offers zero retention on Pro and Enterprise plans [S8].
  - One search snippet says it's "enabled per request" [S6], but the docs dump shows no such request flag.
  - `jev.md` separately advises enforcing OpenRouter's zero-retention setting at the account or API-key level.

  Until a provider confirms otherwise in writing, treat every hosted call as sending the data to that provider.

### 2.3 Open-weight clones: the local-first route (new)

`jev.md` found no open weights and no way to self-host. Several now exist [S10][S11]:

| Project | License | Base model and serving | Reported quality |
|---|---|---|---|
| **Kev** (Jared Palmer) | Apache-2.0 | Current generation: Kev-0.8B, 4B and 9B on Qwen3.5-Base, and Kev-27B. It has its own HTTP server: PyTorch on a GPU or **MLX on Apple Silicon**. It serves `POST /v1/systemone`, and TypeSafe's Python SDK works against it unchanged. **No Ollama or LM Studio support.** | Kev-27B: 0.848 against Jev's 0.857 on new sources; 4B and 9B are about 4 points lower *(builder)* |
| Laya (Convai) | Apache-2.0 | 421M-parameter ModernBERT-large; runs on a CPU | 0.766; ECE 0.081 after fitting |
| open-alternative-jev | Apache-2.0 | Adds Jev-style decisions to any ChatML LLM through HF or vLLM | 0.737; **ECE 0.020**, the best calibration of the group |
| openjev | MIT | NLI cross-encoder on Qwen3.5-4B; needs a GPU | — |

**Added 2026-09-24:** more local candidates to bench against Kev when #22604 is unparked:
- Laya ports for the Mac: mizorewww's MLX and Core ML libraries, and receptron's Node/ONNX port.
- jeff (logan-markewich): a server that TypeSafe's SDK works with unchanged.
- reflex (kshetrajna12): a `/v1/systemone` server that runs on MPS.

Of these, only jeff publishes accuracy: JevBench 66.9 against Jev's 75.3. The §5 rulings stand. Speeds, licenses and fit are in `github-trending-weekly-50-2026-09.md` §1.

**Kev in more detail** [S10]:
- **Mac latency:** on an M5 with 32 GB, Kev-0.8B takes 149 ms on new text and 28 ms when cached; Kev-4B takes 721 ms and 136 ms.
- **Memory:** Kev-0.8B needs about 4 GB. Kev-4B and 9B need a 32 GB Mac.
- **Context:** trained on 384 tokens, and serves up to 8,192.
- **Fine-tuning:** from JSONL labels, and it needs 400 or more examples. On 5,219 complaints Kev-4B went from 0.804 to 0.904 *(builder)*.
- **Limits:** changing the order of the options can change the answer. Calibration is one fitted temperature, which can't reorder confidences.
- **Version churn:** the sizes changed within a week of release. It started as Kev-0.5B on Qwen2.5 (38 MB of LoRA weights), then 0.6B/4B/8B on Qwen3, and now 0.8B/4B/9B/27B on Qwen3.5 [S10][S12]. Treat any size you pick as provisional.

**One comparison's conclusion [S11].** For routing, rank candidates by calibration first: "a miscalibrated 0.9 quietly routes garbage straight to production." Its rule of thumb:
- pay for Jev under about 2 million decisions a month, or when you need the 64k context;
- self-host when data can't leave the machine, when volume is high, or when you need fine-tuning.

**What this means for Gobby:**
- **Same client, several backends.** Kev serves the same `/v1/systemone` API as TypeSafe's direct endpoint. OpenRouter takes the same body at a different path. A client for #22604 whose endpoint URL and model id are configurable reaches all three (§4.1).
- **Local serving isn't Gobby's job.** Kev's server is its own process, and Gobby doesn't install or run local models (memory 606b6838). Where it runs is an installer and user question, the same one as MLX embeddings in the golden-path installer (memory 5d9cd410).
- **Model-instance rule.** The one-model-instance rule (memory a5ad8b5c) applies to any local pilot while the daemon runs.

## 3. Josh's use-case examples against today's code

The #22604 client is where each candidate below would plug in. Two things shape every row:
- **Baseline:** the Gobby LLM call that already does the job (the "Today" column). A decision model has to beat that, not nothing.
- **What it buys:** about 100 ms instead of seconds, calibrated probabilities, and a much lower price.
- **What it costs:** no explanation text; options must be listed up front; it's weak on dates, counting, indirect questions and long irrelevant input.

| Josh's example | Today (file:line) | How often | Fit | Verdict |
|---|---|---|---|---|
| Routing Telegram and inbound messages | Routing is deterministic: reply-to and topic keys (`communications/identities.py:118-128`), pattern rules (`router.py:38`) and opaque button tokens (`telegram_callbacks.py:77`). Every inbound message then runs a full high-profile `chat` turn (`chat_backend.py:137`). | Per message | Poor. Nothing is being classified, and the chat turn still has to run afterwards. A misroute sends Josh's instruction to the wrong session. | **Skip.** One narrow exception: an advisory "needs Josh / FYI" urgency score on outbound notifications, if that becomes a problem. |
| Triaging found work | `FoundWorkStopAnalyzer._confirm_shirk`: an LLM runs only after a regex matches, returns `{block, reason}`, and only `block` is used; 8 s cap (`found_work_gate.py:519-556`) | Per Stop that matches the regex (hot path) | Good. Maps to one Noul question; `jev.md` ranks it #3. | **Yes, after a shadow run.** An outage must not clear a finding. |
| Triaging feedback | The feedback-reviewer agent labels each group defect, guidance-gap, noise or praise (`feedback/agent.py:35`) on `gpt-5.6-sol`, as a nightly cron | Nightly | Fine as a pre-sort, but it's one run a day and the reviewer still has to write the task text | **Low priority.** The savings are negligible. |
| Labeling tasks and lanes | No classifier today. Lane placement is done by hand; this morning's #22508 review was one such pass. | A handful a day | A Choice over the lane charters is possible, advisory only | **Low priority.** The volume doesn't justify it. |
| Classifying memory | The relevance judge (`memory_usefulness`, off by default: `persistence.py:593`; `shadow_relevance.py:258`), dream actions (`memory/dream/planner.py:251`), outdated knowledge-graph relations (`knowledge_graph/extraction.py:229`) | Per turn end (judge, when on); nightly; every 30 min in batches | The judge is the best pilot (`jev.md` #1), with replay, audit and label provenance already built. KG invalidation fits too (#4). Dream actions need synthesis and fit poorly. | **Yes: the relevance-judge pilot.** |
| Sorting hook events | The rule engine is synchronous and deterministic, with no LLM conditions (`workflows/engine/evaluation.py:181`) | Every hook event (hottest path) | Poor as an authority (`jev.md` "Poor fits"). An advisory action-risk tripwire is possible (`jev.md` #6). | **Skip** for rules. The tripwire is optional and later. |
| *Tool and skill reranking* (not in Josh's list) | `recommend_tools` does an LLM rerank on the mid profile (`mcp_proxy/services/recommendation.py:185`, `:242`) | Per call | Good: independent Noul questions give both a ranking and a way to reject every candidate; TypeSafe's own skill-suggestion experiment matches it | **Yes: the likely product win** (`jev.md` #2) |
| *Community labels* (not in Josh's list) | `community_labeler.py:138` generates the name. Specified in #22604 but not wired. | Background, during code-index maintenance | Good: a Choice between the generated name and the deterministic candidates | **Yes: first to build** (§4) |

## 4. What integration would take

### 4.1 Where it sits

- **Not a text-generation endpoint.** `ai.generation.endpoints` only speaks chat-completions and responses (`config/ai.py:47-94`), so a Jev model id configured there would call the wrong API (`jev.md`).
- **No new "fast" tier.** The speed/route axis was removed on purpose in #21655 (memory 3a325faa).
- **Existing capabilities** are embed, audio, vision_extract, text_generate, tool_chat, agent_spawn and web_chat (`ai/registry.py:33-43`). None is a decision capability.
- **`jev.md`'s design:** an `AICapability.DECISION_EVALUATE`, a `DecisionEvaluationService`, an OpenRouter adapter and a `gobby-decisions:evaluate` MCP tool.
- **#22604's plan says less:** one `DecisionsClient` for one consumer, and "Rejected: a general decision-provider abstraction (one consumer; `restraint`)".

**Recommended order**, the least mechanism that still solves the whole problem:

1. **First, when unparked: #22604 as written, with one refinement and one practical note.**
   - Refinement: make the `DecisionsClient`'s endpoint URL and model id configurable, rather than hard-coding a `/v1/systemone` path under `base_url`.
     - TypeSafe direct and a local Kev server take `POST …/v1/systemone`.
     - OpenRouter takes the same `{model, state, questions}` body at its alpha path `/api/alpha/decisions`, with model `typesafe/jev-1.13`.
     - One client then covers all three routes and the wire spike the task already requires (6.3.6), without a second adapter.
   - Question types stay Choice only, as #22604 specifies. The PD ruled this on 2026-09-24 (§5 (d)).
   - Practical note: the wire spike needs a hosted key. TypeSafe direct signups are paused, so it needs an existing TypeSafe account, OpenRouter (`jev.md` says an OpenRouter secret reference already exists) or Vercel.
2. **Second consumer: the relevance judge**, shadow-only, as `jev.md` #1 describes.
   - This is where Noul joins the client: the judge and found-work confirmation are yes/no questions.
   - When it passes, move the five `code_index.community_label.decisions_*` keys into a shared `ai.decisions.*` block that mirrors `ai.embeddings.*`: `api_base`, `api_key` as `$secret:…`, `model` and `timeout_seconds`.
   - Each consumer keeps its own threshold and fallback.
   - Two consumers at that point justify the shared config.
   - Don't add a registry capability or MCP tool until an agent-facing need appears.
3. **Third: tool and skill reranking, then found-work confirmation.** Promote one at a time, each through `jev.md`'s gate on calibration and its own failure policy.

### 4.2 Model and provider

| Route | Privacy | Setup | Quality | When |
|---|---|---|---|---|
| Jev via OpenRouter | Data leaves the machine; account-level zero retention needed | An OpenRouter secret reference exists (`jev.md`) | Reference | Wire spike and the quality ceiling for the pilots |
| Jev direct | Data leaves the machine; zero retention is enterprise-only via sales | **Signups paused** | Reference | Only with an existing account |
| Jev via Vercel AI Gateway | Zero retention on Pro/Enterprise *(secondary source)* | Needs a Vercel key | Reference | Alternative hosted route |
| **Kev-0.8B or 4B, local on MLX** | Stays on the machine | User or installer runs Kev's server; Gobby only points at it | About 4 points below Jev at 4B/9B *(builder)*; can be fine-tuned from 400+ labels | Golden-path installer option; the local-first default |

Run hosted Jev and local Kev on the same frozen labeled set. That produces one number for the gap between them. It's the same method as the D4 embedding eval set.

### 4.3 Cost

- **Tokens:** negligible.
  - #22604 estimates about $0.0003 per labeling pass.
  - A relevance-judge call is about 2k tokens. At an assumed 2,000–5,000 turn-ends a day (not measured), that's around 4–10 million tokens, or roughly $0.20–0.40 a day, hosted.
  - Local Kev costs nothing per call.
- **Engineering:**
  - the client module, plus per-consumer wiring and tests (#22604 already lists them);
  - config migration when the second consumer lands;
  - evaluation harness work per consumer, most of which the relevance judge already has.
- **Operations:**
  - hook-path latency: the found-work confirm now has an 8 s cap, and Jev or Kev would be about 0.1–0.7 s;
  - retry and circuit-breaker handling for 429 and 529;
  - redacted logs;
  - key handling.
- **The decision cost:** whether session, memory or repository text may leave the machine at all. Kev makes "no" a workable answer.

### 4.4 Fit with the golden path

- **Can happen before the golden path:**
  - #22604, which is code plus a keyed wire spike;
  - the relevance-judge shadow evaluation against hosted Jev, if Josh accepts sending data off the machine.
- **Waits for the golden path:**
  - local Kev serving as an installer option;
  - any comparison of local against hosted.

  Both belong with the MLX embedding work (memory 5d9cd410). The PD has already put the D4 embedding pilot behind 0.6.
- **Churn risk:** everything here is 9 days old. Jev's API is alpha on OpenRouter, direct signups are paused, and Kev's sizes changed three times in a week. Pin model versions, and treat an upgrade as a new evaluator (`jev.md`).

## 5. Decisions (through the PD)

The PD's replies of 2026-09-24 are recorded under each item.

1. **Placing #22604.** It's open, has no parent, is unblocked and specified; its epic #22581 closed without it. It carries the `enhancement` label and was detached before #22581 closed, which looked like a parking decision; placing it in a lane would reopen that.
   - **Ruled: stays parked.** Josh on 2026-09-23: "the jev task is enhancement and shouldn't be in the lane epics." No lane placement.
2. **Hosted data policy.** Can memory, session or repository text go to OpenRouter or TypeSafe with zero retention enabled at the account level? If not, pilots wait for a local Kev.
   - **Decided by Josh (2026-09-24, around 13:13 CT, relayed by #14069): no hosted text.** Pilots wait for a local Kev.
3. **Local Kev in the golden-path installer.** Should it sit next to MLX embeddings as the Mac local option? My recommendation: yes, as an opt-in. Kev-0.8B needs about 4 GB.
   - **Decided by Josh (2026-09-24, around 13:15 CT, relayed by #14069):** Kev is an opt-in installer option after the 0.6 golden path, beside MLX embeddings.
4. **Pilot order.** Recommended: #22604 first, then the relevance-judge shadow evaluation, then tool reranking, then found-work confirmation. Skip Telegram routing and hook-event sorting.
5. **(d) Question types in #22604's client: Choice only, or Choice plus Noul?** My pick was to add Noul with the second consumer, per #22604's restraint note.
   - **Ruled: Choice only.** Noul arrives with the second consumer.

## 6. Found along the way

- **`docs/guides/llm-features.md` is missing four feature rows:** `memory_usefulness`, `feedback_review`, `project_verification.synthesis` and `code_index.community_label`, which all have 0 matches (checked with grep on 2026-09-24). #22604 acceptance item 6.3.3 adds `code_index.community_label`; the other three are gaps in the docs. Reported to the PD, who is filing it for a lane.
- **The `code_index.community_label.decisions_*` keys are live config that nothing reads.** They're shown in the config schema and listed in `docs/audits/configuration-audit.md:456-460`. This is expected groundwork for #22604, not a bug. If #22604 is parked for a long time, their descriptions should say "reserved for #22604".

## 7. Sources (all accessed 2026-09-24)

- [S1] AY Automate, Jev builds index: https://www.ayautomate.com/jev-builds
- [S2] AY Automate blog, "Jev / TypeSafe System One model", 2026-09-19: https://www.ayautomate.com/blog/jev-typesafe-system-one-model
- [S3] TypeSafe docs, Agent skill: https://docs.typesafe.ai/agent-skill
- [S4] typesafe-ai/skills repo and SKILL.md: https://github.com/typesafe-ai/skills, https://raw.githubusercontent.com/typesafe-ai/skills/main/skills/typesafe-ai/SKILL.md
- [S5] Anthropic skills repo: https://github.com/anthropics/skills; Claude docs, Agent Skills overview: https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview
- [S6] Web search results on signup status, pricing and retention. They point to: TypeSafe on X (signup pause) https://x.com/typesafeai/status/2102281508950307159; the Vercel changelog https://vercel.com/changelog/typesafe-ai-jev-now-available-on-ai-gateway; jev-ai.live; and requesty.ai. Only the snippets were read, so claims sourced here are unverified.
- [S7] Vercel AI Gateway, Jev model page: https://vercel.com/ai-gateway/models/jev
- [S8] Flavio Copes, "A deep dive into Jev": https://flaviocopes.com/jev/
- [S9] TypeSafe docs, Legal: https://docs.typesafe.ai/legal
- [S10] jaredpalmer/kev: https://github.com/jaredpalmer/kev
- [S11] Rohit Raj, "Open-Weights Jev Alternatives (2026)": https://rohitraj.tech/notes/jev-alternatives-open-weights-decision-models-2026
- [S12] Jared Palmer on X (Kev-0.5B, then the Qwen3 family): https://x.com/jaredpalmer/status/2101028325472841920, https://x.com/jaredpalmer/status/2101715352258232539
- TypeSafe docs index and primitives: https://docs.typesafe.ai/llms.txt, https://docs.typesafe.ai/primitives
- Repository: `docs/research/jev.md` (7624b06efa); task #22604; `.gobby/plans/completed/gcode-import-communities.md` §6.3; memories c176f333, 3a325faa, 5d9cd410, 606b6838 and a5ad8b5c.
