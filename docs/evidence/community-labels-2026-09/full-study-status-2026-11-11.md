# Full decision study status, 2026-11-11

The full D/G/J, carry and shadow sufficiency run started at 17:00 CST and completed
on 2026-11-11. The Gobby-only sample contains 15 communities, seed 22604. Source
rows were read-only and label writes were in memory. No live configuration changed.

The study used local `clef:27b` on `systemone` at `http://127.0.0.1:11434`, with
`response_version` identity `response:clef:27b` and a loaded context of 16384 tokens.
The installed model digest observed before the run was
`039c167bd0ec3719dd055f448dd55ea2365330bc05f2737e042ea0d23659756b`.
Name generation used local LM Studio (`google/gemma-4-26b-a4b-qat`). API charges
for this study were zero; local compute cost was not measured.

| Observation | Count |
| --- | ---: |
| G model labels written | 15 |
| J gate admitted | 15 |
| Shadow sufficiency sufficient / insufficient | 7 / 8 |
| Carry kept / regenerated | 11 / 4 |
| Rating rows | 45 |
| Exact prior member-list/name matches reused | 1 |
| Additional ratings completed by Josh | 44 |

All 45 rows are now rated: 44 new ratings and one exact prior rating. The strict
merge checked item IDs, unchanged displayed columns and integer ratings from 0 to 2.
The [scorer output](full-study-2026-11-11/label-quality-results.md) reports zero G
regressions, accuracy lift 1.6 and findability lift 1.2. Under the original rubric,
carry precision and recall are both 9/11 (81.8%). Confidence separation is undefined
because all candidates were admitted; sufficiency precision and recall are zero.

The original rule-1 recommendation keeps generated labels enabled and all three
decision consumers off. Josh challenged the rating instructions and usefulness of
directory/counter fallback names, then requested Codex-authored names tested with
Clef. Preserve his ratings and the original rubric; this report does not silently
lower the 85% precision bar or relabel partially accurate names as fully accurate.

Both carry false positives received accuracy 1 and findability 1 after perturbation:
“Workflow Execution Infrastructure” (community 5625, keep probability 0.934395) and
“Workflow Definitions and Synchronization” (5253, 0.643777). Both originally had
accuracy 2. The rubric counts only accuracy 2 as a correct keep. A post-hoc definition
requiring both ratings at least 1 instead yields precision 11/11 and recall 11/14;
that sensitivity analysis has not been adopted as an acceptance rule. The runtime
keep cutoff of 0.5 is separate from the study's 85% precision bar.

## Requested Codex/Clef comparison

Codex authored 15 names directly from saved member paths, and local Clef evaluated
them against the original Gemma names. [Method and results](full-study-2026-11-11/README.md)
retain source-blinded Choice in both option orders, usefulness Score and perturbed
membership Noul checks. Choice selected Codex 13 times and Gemma 17 times across
30 answers. One community flipped with option order. Mean usefulness was 1.493682/2
for Codex and 1.498268/2 for Gemma; both retained 7/15 names at probability 0.5.
This diagnostic used complete member lists and does not replace the human-rated
production study or establish carry precision. No generation service authored
the new names, and no live index labels changed.

## Retained artifacts

The original working copies remain outside Git:
`/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/session-21986/full-study-20261111/`.
Completed ratings, key, run and provenance are now retained in
[full-study-2026-11-11](full-study-2026-11-11/). Existing fallback artifacts and all
prior ratings are unchanged. `rating-completion.json` records the completed-sheet
hashes. The additional-sheet hash below describes the original unrated delivery;
the returned rated sheet has its own hash in the completion record.

| Artifact | SHA-256 |
| --- | --- |
| run.json | eb0c8efdef5daaa64de158c7d0b8dd9f0d553ceeabf2d009671df85b8c4b2260 |
| sheet.csv | e5a6efae9e1a732f6500bd6c1a0ae5894734eb635f21dca65f3a33dfe782b684 |
| key.json | 99156e2c29f7211c2b8d798da957a17e5f7cefa61add3d08bb5d7bae4eadf88f |
| sheet.additional-ratings.csv (original unrated delivery) | a5556d3650b9b10dd121973d3720b5b7a077825756c1c4b35977a8713d604d33 |
| sheet.partially-rated.csv | 1bc6480f39a1359d9d26fa701074beeab99a039e55521b784188ed122b6297fc |
| rating-provenance.json | 52b9a2a48ea35143bcaf038cb91673501b91ed2d2ec936b4d45024e542647510 |

## Backend and rollout status

#25819 closed with real registered Box consumer evidence at
`39ce46f8832b0be10c15ff7d3889769627a60075`; see
[Decision shape consumers](../decisions/wire-contracts.md#10-decision-shape-consumers-2026-11-11).
Reported OpenRouter Box cost was $0.0000441.

The initial token-based local boundary probes both exceeded an independently enforced
64 KiB text-and-schema limit and returned 413. The
[byte-boundary follow-up](../decisions/local-clef-byte-boundary-2026-11-11.json)
records 65000 state characters accepted (11965 input tokens), and 66000 rejected with
`text and schema must not exceed 64 KiB`. These establish the effective byte boundary;
they do not establish a token-context truncation contract or an immutable response identity.

Read-only active configuration inspection confirmed generated labels enabled, all three
decision consumers off and their evaluated identities unset. The complete activation
checklist, operator-approved live configuration and V2 maintenance
verification remain open under #25163. No enforcement is authorized by this status report.

The exact original-rubric recommendation is:

```yaml
code_index.community_label.enabled: true
ai.decisions.community_label.mode: off
ai.decisions.community_label_carry.mode: off
ai.decisions.community_label_sufficiency.mode: off
```

These four values match the inspected live state, so accepting this recommendation
requires no mutation. No evaluated identity pins are proposed. An eventual activation
must have its own qualified backend and exact approved patch; its rollback restores
the three modes above to `off`. The Codex diagnostic supplies no activation authority.

## Verification checkpoint

On the unchanged candidate source, the following focused runs passed with
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test` and
`GOBBY_TEST_PROTECT=1`, using `uv run --no-sync pytest ... -q`:

- `tests/ai/test_decisions_service.py tests/code_index/test_community_labeler.py
  tests/config/test_decisions_config.py tests/config/test_runtime_loop_consumers.py
  tests/scripts/`: 246 passed.
- `tests/ai/test_decisions_routing.py tests/mcp_proxy/tools/test_decisions_tools.py
  tests/code_index/test_code_index_maintenance.py`: 82 passed.

`uv run --no-sync ruff check src/ scripts/` passed;
`uv run --no-sync ruff format --check src/ scripts/` found 2244 files formatted;
`uv run --no-sync mypy src/` found no issues in 2228 source files. Project-aware
plan validation passed. Original harness changes had already passed changed-test
quality/types audits and the suppression ratchet; this checkpoint adds only evidence.
Existing anyio and pytest-asyncio deprecations remain a shared-environment finding
handed to its owner; no suppressions were added. The full pytest suite was not run.

Retained artifacts were compared byte-for-byte with their working copies. The
strict scorer was rerun against the retained complete sheet, key and run; its
output matched `label-quality-results.md` byte-for-byte. The initial direct Python
invocation from the task worktree lacked `httpx`; rerunning with the existing main
checkout environment succeeded. The test and lint executables used that existing
environment as shown in their transcripts.

This is a verification checkpoint, not full V2 completion: a live maintenance pass
and the remaining backend activation proofs are still outstanding.
