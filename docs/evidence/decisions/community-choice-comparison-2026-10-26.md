# Community Choice before/after comparison — 2026-10-26 UTC

The deduplicated request used fewer bytes and input tokens and had lower observed
latency. Its answer changed from `generated` to `deterministic_2`, with lower
confidence. Agreement is **0 of 1 distinct community**; this capture does not
establish which answer is better or demonstrate decision-quality equivalence.

The later [OpenRouter ablation](#openrouter-ablation) reproduced the flip and
isolated restoring `generated.name` as sufficient to recover the generated
choice for this frozen community. The implementation now retains that field.

## Scope and provenance

The Assistant ran the same measurement script against two exact Git revisions.
The script archived each revision's source and built the request through the real
`CommunityLabeler._gate` and `DecisionService.choose` path. The input was frozen
community `43`, member signature `1bb6768c2c238cdb`, with 53 members and five
representatives. **BEFORE and AFTER are persisted-label replays:** the generated
label is `Chat Session Management`, the original rationale is unavailable, and
the supplied rationale is the empty string `""`. Neither run generated a fresh
label or wrote a community label to the database.

- BEFORE revision: `8734a61723548a1a9ef0d1f1599664091ba88ea1`.
- AFTER revision: `7d797424d7de2468acdf1273165d23e7083cf354`, landed and activated
  before the AFTER measurement.
- Script SHA256: `847d809b5af2f1d52afa7c461a12a8c104bd8596db4883a7dcdd6407451a6b0e`.
- Fixture SHA256: `ab68e6adf92f2a7513e9d2e3a6ea25249696d3a74dba5686388e7bab96740191`.
- Endpoint: LM Studio `http://127.0.0.1:1234`, `/v1/systemone`, model alias `clef`;
  per-call timeout 15 seconds. Responses report model `clef`; the captured
  `backend_identity` is `null`.

Each phase contains one warmup and 20 accepted measured calls of the **same single
community**, not 20 independent quality examples. “Accepted” means successful and
not excluded by the measurement script; it is not a label-quality judgment.

## Observed measurements

| Measurement | BEFORE | AFTER |
| --- | ---: | ---: |
| Compact UTF-8 request bytes | 2,527 | 2,272 |
| Application token estimate | 651 | 585 |
| Returned `usage.input_tokens` | 703 | 651 |
| Returned `usage.output_tokens` | 0 | 0 |
| Cached-token count | absent; recorded `null` | absent; recorded `null` |
| Warmups / accepted measured calls | 1 / 20 | 1 / 20 |
| Errors / timeouts / HTTP 504s | 0 / 0 / 0 | 0 / 0 / 0 |
| Observed Nomic overlap / excluded calls | 0 / 0 | 0 / 0 |
| Latency p50, seconds | 2.256206 | 1.879900 |
| Latency p95, seconds, nearest rank | 2.555964 | 2.153159 |
| Latency minimum–maximum, seconds | 2.028326–2.748335 | 0.952270–2.291634 |
| Selected answer, all 20 measured calls | `generated` | `deterministic_2` |
| Selected label | `Chat Session Management` | `useChat` |
| Confidence, all 20 measured calls | 0.8487084001485312 | 0.5189132749083801 |

Every measured answer and its probability vector was identical within its phase:

| Option | Label text, unchanged between requests | BEFORE probability | AFTER probability |
| --- | --- | ---: | ---: |
| `generated` | `Chat Session Management` | 0.878966720118825 | 0.1481495739780211 |
| `deterministic_0` | `web/src/hooks/useChat` | 0.038304691343661976 | 0.12904572201962675 |
| `deterministic_1` | `chat` | 0.025706925792801958 | 0.10353173059576197 |
| `deterministic_2` | `useChat` | 0.05430946916463743 | 0.6151306199267041 |
| `deterministic_3` | `src` | 0.002712193580073527 | 0.004142353479886068 |

Warmup is excluded from latency statistics. p50 is the median; nearest-rank p95
is the 19th sorted latency among 20 measured calls. Nomic status was sampled before
and after each call; zero observed overlap does not exclude activity between those
snapshots. The requests were repeated without a nonce, and no cached-token count
was returned, so this is not proof of cache-free latency. The phases ran at
different times; their observed latency difference alone does not isolate the
effect of the payload change.

## Exact payload difference

The preserved [BEFORE request](community-choice-2026-10-26.before.json) and
[AFTER request](community-choice-2026-10-26.after.json) contain the captured JSON
objects. Request SHA256 is calculated after compact UTF-8 serialization with
`ensure_ascii=False`, separators `(",", ":")`, and `allow_nan=False`; it is not
the hash of the indented artifact file.

| Phase | Compact request SHA256 |
| --- | --- |
| BEFORE | `9d5f7a7691322671ac5db561bc00db4db898d08e1551558ef9464e0a480e360a` |
| AFTER | `c05f4b2033362a45a64458cba41ae311376b62e621ac4bba421764d50c0e8b4b` |

All 21 captured payloads within each phase produce its listed hash. The only
changes are inside `state.communities.c43`:

1. `representatives` is unchanged, including order. The following five paths were
   removed from the **front of `members`**, where they duplicated that list:

   ```text
   web/src/types/chat.ts
   web/src/hooks/useChat/sessionRecords.ts
   web/src/hooks/useChat/conversationPersistence.ts
   web/src/hooks/useChat/actionTypes.ts
   web/src/hooks/useChat/pendingProxyMessages.ts
   ```

   `members` shrank from 40 to 35 entries. Those remaining 35 retain exactly their
   prior order. The union of representatives and members is the same 40 unique
   paths in both requests; no distinct file evidence was removed or added.
2. `candidates` was removed. Its four strings are still present verbatim in
   `questions.c43.criteria.deterministic_0` through `deterministic_3`, with the
   same order and mapping shown above.
3. `generated.name` was removed. Its exact text, `Chat Session Management`, is
   still present in `questions.c43.criteria.generated`. The `generated` object
   retains `rationale: ""`; the option's text was not removed from the request.
4. `member_count: 53` and `omitted_member_count: 13` were added. They describe the
   same selected subset; these 13 omitted paths were also absent BEFORE.
5. State key order changed from `representatives, members, candidates, generated`
   to `representatives, members, member_count, omitted_member_count, generated`.
   Model, question key `c43`, type, instructions, criteria text and criteria key
   order are unchanged. No other payload field changed.

## Interpretation and decision-quality agreement

The diff implements the intended deduplication and omission disclosure. It shows
no loss of unique path or option-label text, and no answer-key remapping defect.
Removing duplicate text still changes its repetition and position; the new counts
also change context. Those differences could affect a language model's decision,
but these two captures contain no ablation that identifies which change caused
the flip.

The observed flip and confidence reduction remain a quality-agreement finding:
agreement is 0/1 distinct frozen community. Repeating that input 20 times per phase
does not turn it into a 20-community quality study. No blinded human rating in this
capture establishes whether `Chat Session Management` or `useChat` is preferable.
The lower byte/token counts and observed latencies therefore do not establish
preserved quality or justify a broader quality/ship decision. No production change
was made in response to this comparison.

## Capture locations and offline verification

The Assistant's complete original run directories contain `summary.json`,
`row-0.json` through `row-20.json`, and matching `payload-N.json` files:

```text
BEFORE /var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-22604-choice-before-_4nfwy_r
AFTER  /var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-22604-choice-after-sfrnuqbd
SCRIPT /private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-lag7b7s1/task-22604-proof/measure_real_choice.py
```

| Original file | SHA256 |
| --- | --- |
| BEFORE `summary.json` | `b8d44529e9d2af13ac1a672e2a01abfcfe9178ff5759d890a8e1d33a1edbfcca` |
| AFTER `summary.json` | `5ca027528450028c144d543ef6ec0017a24a7e021457cbee870ac1e899b93185` |

Offline verification read all 42 captured payloads and rows, recomputed request
hashes, sizes and latency percentiles, checked the reported usage and answer
vectors across all accepted rows, and asserted the exact field/path relationships
listed above. The two request artifacts here are indented copies of each run's
`payload-1.json`, identical after parsing, with one trailing newline added. This
documentation pass made no live inference calls.

Measured latencies in seconds, in attempt order 1–20, are preserved here so the
percentiles remain independently recomputable without the temporary directories:

```json
{
  "before": [
    2.2623134170426056, 2.14725024998188, 2.327911124972161, 2.300365958013572,
    2.1137917499872856, 2.3163332500262186, 2.2613507920177653, 2.5559635829995386,
    2.290391458023805, 2.3964462089934386, 2.0283256659749895, 2.0320339579484425,
    2.068992624990642, 2.1741319579887204, 2.1099391249590553, 2.1192929170210846,
    2.7483349579852074, 2.230599958973471, 2.251060333044734, 2.4364722500322387
  ],
  "after": [
    2.153159332985524, 1.9988843339961022, 2.122566625010222, 1.8068297919817269,
    1.8639495419920422, 1.895849917025771, 1.9966743750264868, 0.9522701249807142,
    1.8958593330462463, 1.8598345829523169, 0.9672441669972613, 2.0180081659927964,
    0.9526755830156617, 0.957054624974262, 0.9603473339811899, 0.9774301249999553,
    0.9835984170204028, 2.2916335000190884, 2.082480625016615, 1.9001301670214161
  ]
}
```

## OpenRouter replication

The operator/Assistant ran nine calls against
`https://openrouter.ai/api/alpha/decisions`, pinned to
`typesafe/jev-1.14-20261014`: three Score calls and three paired BEFORE/AFTER
Choice comparisons. The frozen c43 state and questions above were unchanged;
each phase used the dated OpenRouter model and added provider privacy options
(`zdr: true`, `data_collection: deny`). There were no warmups or retries, and each call had
a 30-second deadline. All nine returned HTTP 200 and the pinned model.

| Choice phase | Selection, all three calls | Generated probability | Confidence | Input tokens | Request bytes |
| --- | --- | --- | --- | ---: | ---: |
| BEFORE | `generated` | 0.77 / 0.75 / 0.81 | 0.71 / 0.69 / 0.76 | 1,111 | 2,598 |
| AFTER | `deterministic_0` | 0.23 / 0.25 / 0.25 | 0.16 / 0.16 / 0.14 | 1,023 | 2,343 |

The flip reproduced on a second backend, with AFTER near a tie between
`deterministic_0` and `deterministic_2`. The nine calls cost $0.000308952 as
reported by the endpoint. The [original capture summary](community-choice-openrouter-2026-10-26.json)
preserves every request, response, timestamp, latency, usage and parser result.
Its bytes are unchanged from the operator artifact; SHA256:
`782b5a232019fb6e8866511214262eb58b1913cc984c7cd08e83b534efe72ac7`.
Capture script SHA256:
`affaf5f803c22825c5e0f7296fbd436319a51864fb27665bcfbc8705b0884d88`.

## OpenRouter ablation

The Assistant then ran three calls per arm, 21 total, with the same pin,
deadline and privacy settings, no warmups and no retries. The schedule rotated
arm order between rounds. Each intervention reverted only its named feature
of AFTER; the order-only arm changed key order without changing JSON values.
All 21 returned HTTP 200 and the pinned model; total reported cost was
$0.000924462. One response failed strict answer validation.

| Arm | Returned choice, attempts 1 / 2 / 3 | Generated probability range | Confidence range | Input tokens | Bytes |
| --- | --- | --- | --- | ---: | ---: |
| BEFORE control | generated / generated / generated | 0.78–0.80 | 0.73–0.75 | 1,111 | 2,598 |
| AFTER control | d0 / d0 / d0 | 0.23–0.24 | 0.15–0.16 | 1,023 | 2,343 |
| Restore candidates | d0 / d0 / d0 | 0.15–0.18 | 0.21–0.25 | 1,052 | 2,405 |
| Restore generated name | generated / generated / generated | 0.83–0.86 | 0.79–0.82 | 1,033 | 2,376 |
| Restore member duplicates | d0 / d0 / d0 | 0.25–0.28 | 0.12–0.16 | 1,090 | 2,547 |
| Remove both counts | d2 / d0 / d0 | 0.22–0.26 | 0.14–0.15 | 1,005 | 2,299 |
| Order only | d0 / d0 / d0 | 0.22–0.27 | 0.18–0.19 | 1,023 | 2,343 |

Here `d0` and `d2` abbreviate `deterministic_0` and `deterministic_2`.
Row 14, AFTER attempt 2, returned d0 at probability 0.31 while d2 had 0.32.
The strict parser correctly rejected it with `choice is not the most probable
option`. It remains a rejected observation, not an accepted d0 decision.
No Choice parser relaxation follows from this experiment.

Restoring only `generated.name` was sufficient to recover the generated
choice in all three calls. None of the other single reversions did so. This
supports retaining the generated option's exact text alongside its rationale,
while keeping candidates omitted from state, representative deduplication and
both counts. The restored name costs 33 bytes and 10 measured input tokens
relative to AFTER. The existing state and request bounds still apply to it.
This is one persisted-label replay with an empty rationale, not independent
quality examples or a blinded quality result. It does not establish
interactions among changes or improvement over BEFORE. The actual repaired
builder still needs its operator-run comparison on both backends.

The [original ablation summary](community-choice-openrouter-ablation-2026-10-26.json)
preserves all 21 rows, intervention definitions, schedule and request hashes.
Its bytes are unchanged; SHA256:
`164eb5af1e45dd985b6f5b97d929489c41832976b28ef7d516531bc578facd3b`.
Ablation script SHA256:
`90d27dcce0d42fe152a498ab3b7b8533cc7e79bb47f2d1035f18355c17ccd23a`;
it used the replication script identified above.

## OpenRouter Score rounding

Replication row 7 returned score 1.95 with probabilities
`{"0": 0, "1": 0.04, "2": 0.96}`, whose weighted index is 1.96.
The original parser rejected the 0.01 difference against its 0.002 tolerance.
The first two Score calls matched their weighted indices exactly. The retained
artifact records that original rejection; it has not been rewritten as GREEN.

For normalized probabilities rounded to hundredths, each rounding error is at
most `h = 0.005`, and the errors sum to zero. With weights `0..n-1`, allocating
positive errors to the largest weights and negative errors to the smallest
gives the bound `h * floor(n²/4)`. Rounding the returned score adds another
`h`, giving `0.005 * (floor(n²/4) + 1)`: 0.01, 0.015, 0.025 and 0.13 for
2, 3, 4 and 10 levels. This assumes nearest rounding; the captured hundredth
grid motivates a compatibility profile, not a claim about guaranteed vendor
precision or its internal probabilities.

The parser applies this bound only when the score and all probabilities lie
on that grid, allowing `1e-12` representation slack. Higher-precision answers
retain `(n-1) * 0.001`; all type, key, legend, range and probability-sum checks
remain in place. The capture diagnostic uses the same tolerance helper.
Tests preserve the captured 1.95 response, both sides of the bounds, floating
representation at the bounds, higher-precision guards, and rejection of
1.94 against the same 1.96 weighted index.

_Last verified: 2026-10-26_
