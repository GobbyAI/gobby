# Activation Gate item 4: gap captures, 2026-10-26

**Item 4 remains unmet on both wires.** The approved probe sequences completed;
completion is not activation approval. No live configuration or community label
was changed. The selected full D/G/J and carry study, Josh's blinded ratings,
the ship decision, V2 checks and real Box consumer evidence remain outstanding.
The fallback study package remains inactive.

The [machine-readable checklist](item-4-checklist-2026-10-26.json) normalizes
the evidence into `met`, `unmet` and `unverified` items. A met observation does
not pass the whole gate. Neither wire may leave shadow on this evidence.
Governing criteria: `.gobby/plans/decision-classifier-path.md:958–992` and
`.gobby/plans/community-label-decisions.md:787–830`.

## Runs and preserved artifacts

The Assistant ran each command once, operator-side, after reading and hashing
the approved worktree copies. Durable execution receipt:
`2671ce29-fd05-406a-b1f4-03e20082cb09`. Both processes exited 0. There were no
retries or additional inference calls during evidence intake.

| Artifact | Capture time (UTC) | Calls | Result |
| --- | --- | --- | --- |
| [OpenRouter gap capture](openrouter-item4-2026-10-26.json) | 22:41:36 | 1 catalog GET, 11 inference POSTs | Ten POSTs returned 200; Box returned 400 |
| [LM Studio gap capture](lmstudio-item4-2026-10-26.json) | 22:41:46 | 1 catalog GET, 2 Noul POSTs | All returned 200 |
| [LM Studio model metadata](lmstudio-models-2026-10-26.json) | 20:44:23 | 1 earlier authorized GET | `clef-flash`, Q8_0, loaded, context 262144 |

These JSON files preserve the original bytes; SHA256 values are in the
checklist. The metadata GET was separate from the three-call LM probe run.
Captures retain request hashes, sizes, statuses, selected usage and parsed
answers. They omit authorization headers, raw error bodies and image bytes.
The only credential reference is the stored name `openrouter_api_key`.

The harness SHA256 was
`7d4b7f6c6cf9393b5a7d733af8b0d09f4753a918f5a61f5c9f6c96be96f452c0`;
the instruction-file SHA256 was
`47b90e8336047ebf07d850d005db26252eb486dbd1ce3d222503bcb29ceb6841`.
The source remains in the developer's session workspace, outside this commit.
Both captures record parser SHA256
`9a6a8147e81575baa16610242628b037cf6faf0029c080214104dbfda4ee6f8f`.

The Orchestrator authorized temporary untracked copies under `scripts/`
because the Assistant could not read the developer workspace. To preserve
its hash, the copied instruction file retained its old workspace paths;
message `44b6b45a-da1d-43ed-8c04-bbedff40b017` supplied replacement commands
using worktree paths. After execution, the Assistant's write scope prevented
cleanup. The developer verified the hashes and removed only
`scripts/item4_probe.py` and `scripts/item4-operator-instructions.md`.
The original capture JSONs and `scripts/__pycache__/` remain ignored.
No `scripts/` path belongs to this evidence commit.

## OpenRouter findings

The catalog's `canonical_slug` and every successful response identify
`typesafe/jev-1.14-20261014`. Alias `~typesafe/jev-latest` resolved to that
different, versioned name. This verifies the capture identity, not Gobby's
live configured pin, which was not remeasured.

Choice and Score reuse the committed
[repaired capture](community-choice-openrouter-repaired-2026-10-26.json)
and [comparison](community-choice-comparison-2026-10-26.md): six parsed Choice
responses and three Score responses, 1.96/1.97/1.96, at the same pinned version.
The gap run adds parsed Noul responses without rerunning those shapes.

State and question calibration each added 4096 synthetic path-heavy characters
and 1686 reported input tokens: **2.4294187425860025 characters per token**.
This measures the sampled pattern, not a universal tokenizer ratio or complete
token-accounting contract.

| Raw probe name | Questions | Reported input tokens | HTTP |
| --- | ---: | ---: | ---: |
| `question_under` | 1 | 32122 | 200 |
| `question_over` | 1 | 33433 | 200 |
| `request_under` | 4 | 63445 | 200 |
| `request_over` | 4 | 66061 | 200 |
| `header` | 1 | 33433 | 200 |

Synthetic targets were 32768 and 65536 tokens. `question_under` is already
above 32000; its name does not prove success below a decimal 32k ceiling.
Both over probes were accepted. No rejection or DecisionService `truncated`
result was recorded. The header probe and `question_over` used identical
body hashes and returned identical usage. Acceptance does not prove the
backend honored the header.

OpenRouter documents automatic long-context routing above approximately
32000 text **bytes**, and for image requests. These larger probes cannot
isolate default-mode enforcement. Its catalog advertises 262144 tokens.
[OpenRouter Jev guide](https://openrouter.ai/docs/guides/community/jev),
retrieved 2026-10-26.

TypeSafe's documented scopes are state plus every question per request, and
state plus one question per question. Its direct endpoint defaults are
64k/32k, with an explicit header enabling 262k. This alone does not establish
OpenRouter's routing or complete usage accounting.
[TypeSafe models](https://docs.typesafe.ai/models), retrieved 2026-10-26.

The API reference exposes usage, input-token and cost examples, and HTTP
errors including 400 and 413. This review did not establish complete
serialization, multimodal accounting and truncation semantics.
[OpenRouter Decisions API reference](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request),
retrieved 2026-10-26. The limit/config match, complete-usage contract and
boundary-enforcement items remain **unverified**.

The synthetic 64×64 PNG Box attempt returned **HTTP 400**. The retained error
code is `unclassified`; raw error text was deliberately omitted. No Box
answer reached the parser. This does not prove the parser rejected a valid
answer or that Jev lacks Box support. The successful round trip and failure
cause remain unresolved; #24248's required real consumer evidence is also
outstanding.

Every inference record includes `zdr: true` and `data_collection: deny`.
This records sent flags, not an audit of provider retention.

The approved cap was **$0.05**, at most 12 HTTP calls, 30 seconds per complete
exchange, no retries. Successful responses report **$0.010082184** total.
The Box error has no captured cost, so the harness retained a full-context
reservation of **$0.011010048**. Raw `cost_usd: 0.021092232` is conservative
budget accounting, **not a measured invoice total**. The actual Box charge
is unknown; no additional charge lookup was made.

## LM Studio findings

Selected-model metadata reports `clef-flash`, `Q8_0`, loaded, with maximum
and loaded context 262144. Serving `/v1/models` supplied no retained source
identity fields. Echoing `clef-flash` establishes neither a pinned version,
source commit nor model-card contract. Identity is **unmet**.

Two Noul results parsed as probabilities 0.9758969379261029 and
0.9744102746319381. Usage rose from 1838 to 3524 for 4096 added state
characters. No question-only, image, header or full-context boundary probe
ran. The loaded context exceeded the harness's 16384-token local boundary
ceiling, so that prefill was deliberately skipped. Complete usage and limit
semantics remain **unverified**.

Earlier local Choice evidence remains historical; an earlier `clef` run is
not silently credited to `clef-flash`. Choice, Score and Box at this selected
identity are unverified by the gap run. Its three calls used 15-second
deadlines and no retries. API charge was zero; local compute cost was not
measured. LM Studio remains shadow/evaluation only. Its gate failure alone
does not block the study; OpenRouter's unmet gate still blocks enforcement.

## Raw-report interpretation corrections

Original raw bytes preserve these reporting defects:

- Both captures serialize `plan.live: false`, the offline-plan default,
  despite operator execution with `--run-live`. The receipt and response
  records establish live runs.
- OpenRouter marks both over probes `met` with generic rejection/guard
  wording, but both returned 200. Enforcement remains unverified; no
  truncation proof is awarded.
- OpenRouter `cost_usd` includes the unknown-cost Box reservation.
- LM Studio default-limit/header descriptions reuse OpenRouter boilerplate.
  They establish nothing about local routing or header support.
- `box_parsed: unmet` denotes no parsed result after HTTP 400, not a parser
  rejecting a successful response.

The Dev Manager approved correcting interpretation here while preserving
raw bytes. No rerun was authorized or made.

## Defaults and validation provenance

The [defaults ship check](community-choice-comparison-2026-10-26.md#defaults-ship-check-already-active-2026-10-26)
was complete as a no-op at reported revision 157. Desired, active and target
values matched; no PATCH or rollback receipt was created. The old temporary
helper and checker sources were lost when their directory disappeared after
the daemon interruption. Their hashes and durable receipts survive; those
bytes are not claimed available or reconstructed. This no-op does not
activate consumers or settle item 4.

Before operator execution, the harness passed 19 focused offline tests in
2.18 seconds, isolated Ruff and scoped mypy. Initially 13 tests failed with
`NotImplementedError` against a behavior-free stub, then passed after
implementation; six additional guard regressions also passed. Original RED
evidence is retained and was not replayed. Final harness command:

```sh
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 PYTHONDONTWRITEBYTECODE=1 uv run pytest /Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/developer/session-17556/test_item4_probe.py -q -p no:cacheprovider --basetemp=/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/developer/session-17556/pytest-release
```

Test-quality and test-types audits refused the outside-project path; they
are not credited as passes. The suppression audit passed: 4745 files, 169
baseline entries, zero new or stale entries. These are historical checks,
not reruns during this evidence-only change.

Final evidence validation checks strict JSON, artifact hashes, counts/cost
arithmetic, checklist status values, local links, credential patterns and
the exact staged paths. No production behavior or tests change in this commit.

_Last verified: 2026-10-26_
