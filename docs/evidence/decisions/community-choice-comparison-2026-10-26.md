# Community Choice before/after comparison — 2026-10-26 UTC

The deduplicated request used fewer bytes and input tokens and had lower observed
latency. Its answer changed from `generated` to `deterministic_2`, with lower
confidence. Agreement is **0 of 1 distinct community**; this capture does not
establish which answer is better or demonstrate decision-quality equivalence.

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
listed above. The two request artifacts here are byte-for-byte copies of each
run's `payload-1.json`. This documentation pass made no live inference calls.

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

_Last verified: 2026-10-26_
