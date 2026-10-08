# Cloudflare Clef: Jev alternative and Gobby fit

Research for #23788 (Cloudflare open-source alternative), assessed 2026-10-08.
Owner: Lane 7 Plan Adversary 4, gobby#15471. Research only; no integration or benchmark run.

## Finding

Josh meant **Clef**, Cloudflare's decision-model family, with **Clef-flash** as its
smaller variant. Cloudflare's October 1, 2026 [announcement](https://blog.cloudflare.com/clef-decision-models/)
explicitly positions both against TypeSafe AI's Jev. The identity is resolved.

The primary release repositories are [Cloudflare/clef](https://huggingface.co/Cloudflare/clef/tree/main)
and [Cloudflare/clef-flash](https://huggingface.co/Cloudflare/clef-flash/tree/main) on
Hugging Face. They contain weights, the joint decision head, inference code
(`joint_schema_model.py`), processors, and Apache-2.0 licenses. The announcement
links these repositories. Cloudflare's [GitHub LangChain integration](https://github.com/cloudflare/langchain-cloudflare)
is a client integration rather than the model release repository.
License evidence: [Clef LICENSE](https://huggingface.co/Cloudflare/clef/blob/main/LICENSE)
and [Clef-flash model card](https://huggingface.co/Cloudflare/clef-flash#license).

## What it replaces

Clef replaces a **bounded classification or scoring call**, including Jev-shaped
decision calls or an LLM coerced into returning fixed choices. It receives state
plus typed questions (`choice`, `score`, `noul`) and returns probabilities over
allowed answers in one forward pass. Clef is 27B; Clef-flash is 9B. These outputs
cannot generate an arbitrary explanation, community name, plan, or code.
See the [Clef model card](https://huggingface.co/Cloudflare/clef#clef) and
[TypeSafe introduction](https://docs.typesafe.ai/introduction).

Request/response compatibility does not mean identical transport. TypeSafe uses
`POST /v1/systemone`; hosted Clef uses Cloudflare's account-specific Workers AI
`/ai/run/@cf/cloudflare/clef` endpoint, a Cloudflare credential, and `model: clef`.
An adapter must validate the Cloudflare REST envelope, errors, and model identity.
Compare the [TypeSafe API](https://docs.typesafe.ai/api),
[Cloudflare endpoint example](https://developers.cloudflare.com/workers-ai/models/clef/),
and [Workers AI REST envelope](https://developers.cloudflare.com/workers-ai/get-started/rest-api/).

## Does "better" hold?

Cloudflare reports these results from its own evaluation runs; they are not Gobby
measurements or an independent replication. The comparison is workload dependent.

| Published measure | Clef | Clef-flash | Jev |
| --- | ---: | ---: | ---: |
| Median request latency, ms | 209.3 | 38.8 | 524.1 |
| p95 request latency, ms | 238.6 | 122.4 | 536.0 |
| Agent-trace observability, primary-action accuracy, % | 68.5 | 69.8 | 71.6 |
| CLINC150+OOS, macro-F1, % | 97.4 | 66.8 | 89.3 |

Source: [Cloudflare model-card results](https://huggingface.co/Cloudflare/clef#results).
Clef has promising reported latency; Jev wins the trace-observability row, and
Flash scores much lower on intent classification with out-of-scope cases. A schema-valid answer
can still be the wrong decision.

| Current hosted property | Clef | Clef-flash | Jev 1.13 |
| --- | --- | --- | --- |
| Price per million input tokens | $0.24 | $0.09 | $0.042 |
| Context budget | 65,536 tokens | 65,536 tokens | 64k total; 32k state plus longest question |
| Inputs | Text/JSON and embedded images | Text/JSON and embedded images | Text/JSON |

Sources: [Clef docs](https://developers.cloudflare.com/workers-ai/models/clef/),
[Flash docs](https://developers.cloudflare.com/workers-ai/models/clef-flash/),
[current TypeSafe models](https://docs.typesafe.ai/models).
Cloudflare's advertised 64k-versus-32k advantage needs this qualification.
At equal input-token counts, Clef costs about 5.7 times Jev and Flash about 2.1
times Jev; overall workflow cost still depends on token accounting and retries.

The hosted Clef docs allow 1–64 questions and truncate long text state to the
model limit. They document up to four embedded images and reject remote image
URLs. The local model card also describes video and defaults `encode_record`
to 16,384 tokens. Do not infer hosted video support or a local 64k default from
the family headline. See [hosted parameters](https://developers.cloudflare.com/workers-ai/models/clef/#parameters)
and [local encoding](https://huggingface.co/Cloudflare/clef-flash#input-format).

## License, maturity, and adoption

The released weights and inference implementation are Apache-2.0, enabling a
self-hosted experiment. This does not establish that Cloudflare released its
training datasets or a reproducible full training pipeline. Hosted service terms
are separate from the downloadable release license.

The public release is seven days old. The [Clef repository history](https://huggingface.co/Cloudflare/clef/commits/main)
and [file listing](https://huggingface.co/Cloudflare/clef/tree/main) show 11 commits
and four contributors as observed today, including API-helper work on release day
and a later metadata update. The model repositories list about 55 GB for Clef and
19.1 GB for Flash; those are repository sizes, not measured RAM requirements.
The cards report testing on an H200, which is not evidence of acceptable
performance on Josh's Mac. [Flash files](https://huggingface.co/Cloudflare/clef-flash/tree/main),
[tested setup](https://huggingface.co/Cloudflare/clef-flash#usage).

Adoption evidence is early: Cloudflare reports internal threat-intelligence
testing; its maintained LangChain package added Clef in 0.4.0 on October 1 and
released 0.4.1 on October 3. That demonstrates integration activity, not established
production reliability for Gobby. The integration's changelog also reports native
REST rejection of `options.rejectIfBusy` with HTTP 422 and no dynamic routing for
Clef decision models in that release. These are upstream observations, not failures
run against Gobby. [Cloudflare announcement](https://blog.cloudflare.com/clef-decision-models/),
[official integration changelog](https://github.com/cloudflare/langchain-cloudflare/blob/main/CHANGELOG.md).

## What Gobby actually uses

This is a checkout assessment, not a claim about installed runtime configuration.
The following evidence was read with `gcode evidence`, version 1.9.15, bound to
commit `76f806bf7509c18718acdfbf37a427186e16b48a`, tree
`63f2fd0fc1e30f90b8a73230b2e9ee54b5b40fbd`, project
`d45545c5-ded5-4335-b115-0245752edacf`. Each response was complete with no warnings.

- [Community label configuration](../../../src/gobby/config/code_index.py#L35)
  exposes optional Decisions API settings, defaults the base URL and key to
  `None`, sets `decisions_model="jev-latest"`, and sets a 0.5 confidence floor.
  This is configuration intent; it does not prove a live Jev call.
- [CommunityLabeler](../../../src/gobby/code_index/community_labeler.py#L68)
  reads the text-generation profile/candidates, calls `generate_json` for
  `name` and `rationale`, then accepts a deterministic candidate or sanitizes
  a generated name. It stores `label_confidence=None`. The inspected class
  does not consume the Decisions API configuration. A Clef choice could select
  from existing label candidates or judge a generated label, but cannot produce
  arbitrary new names and rationales. Replacing the entire generator would lose
  existing behavior.
- [FoundWorkStopAnalyzer._confirm_shirk](../../../src/gobby/workflows/found_work_gate.py#L522)
  is an existing bounded classifier: it asks `call_json_feature` for a boolean
  `block` verdict, caps the call at eight seconds, and returns `None` when the
  service is unavailable or the result is malformed. Its documented fallback
  uses the fast-path verdict. This is a closer functional fit, but changing an
  enforcement decision before domain evaluation would be premature.

| Evidence range | Evidence ID | File SHA-256 |
| --- | --- | --- |
| `src/gobby/config/code_index.py:35–80` | `src:269d45dc42635785f20c130bd22b5ce507304d55d66ff09a8b99bc28480b34e4` | `204ed15dadb96f78dd6f83d482f27df9fad62d5b624baae34c86bc94ff85cc79` |
| `src/gobby/code_index/community_labeler.py:68–188` | `src:1d9d9614234cdb6c61d2df7ef09d1a891ad1187bed16cbfe01a883d3db99aa08` | `c1a90094907f1f3cdba6839b8e728d98432cd726d292e28003aa7d371e945e18` |
| `src/gobby/workflows/found_work_gate.py:522–559` | `src:04912aad12ff5da39747f62f8c60d49d4d147c9e7084f02d9b489cf4797ded80` | `7783e0f57aadeaa8112e7a9f739c47225559ac7779cb2412f028e65a3ed91a0e` |

Reproduce a range with:

```sh
gcode evidence --request-json '{"schema_version":1,"operation":"read","binding":{"project_id":"d45545c5-ded5-4335-b115-0245752edacf","commit_oid":"76f806bf7509c18718acdfbf37a427186e16b48a","tree_oid":"63f2fd0fc1e30f90b8a73230b2e9ee54b5b40fbd"},"read":{"kind":"range","path":"src/gobby/config/code_index.py","start_line":35,"end_line":80}}'
```

## Recommendation: trial

Trial Clef and Flash as **offline decision classifiers**, starting with selection
among existing community-label candidates. Keep the current generator as the
baseline. Add archived stop-classification cases only as a separate evaluation
set; do not let trial outputs alter hooks, task state, close admission, or policy.
This is a proposed follow-up experiment, not implementation authorized by this note.

Use a fixed, human-labeled holdout containing ambiguous, out-of-scope, adversarial,
and long-input cases. Compare the existing LLM path, pinned Jev 1.13, Clef, and
Flash on the same state/questions. Measure accuracy, false decisions, abstention
coverage, probability calibration, p50/p95 wall-clock latency, token cost, and
timeouts/retries. Test endpoint envelopes and question-schema differences;
provide explicit instructions and descriptions rather than relying on undocumented
compatibility. A probability-shaped response does not establish calibration on
Gobby data. TypeSafe itself [defines confidence as a distribution statistic](https://docs.typesafe.ai/confidence)
and says thresholds depend on the domain.

Advance only if a candidate matches or improves held-out decision quality at the
same abstention coverage, stays within the existing timeout budget, and improves
measured latency or total cost. Preserve deterministic sanitation and unavailable
service behavior. Pin model/artifact versions and rerun the holdout before upgrades;
do not transfer Jev's confidence threshold without calibration. Start with approved
sanitized state over the hosted endpoint; evaluate self-hosting separately before
installing runtimes or downloading large weights.

**Trial** is justified by open weights, a compatible decision shape, and promising
vendor latency. Production adoption is unsupported by this one-week-old release,
mixed quality results, higher hosted token prices, and Gobby's need to retain free
text generation. No immediate provider swap or new runtime dependency is recommended.
