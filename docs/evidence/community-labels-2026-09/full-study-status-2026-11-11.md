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
| Additional ratings pending from Josh | 44 |

These are execution observations, not quality verdicts. No quality bars, precision,
recall or ship decision are claimed before the remaining ratings arrive.

## Private retained artifacts

Until ratings finish, retain the sheet, key, run and reuse provenance outside Git:
`/Users/josh/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/session-21986/full-study-20261111/`.
Only `sheet.additional-ratings.csv` is delivered to Josh. Existing fallback artifacts
and all prior ratings are unchanged. The partial sheet contains the one reused rating;
merge the returned additional sheet by item ID, verifying unchanged displayed columns,
then run the existing strict scorer on the complete 45-row sheet.

| Artifact | SHA-256 |
| --- | --- |
| run.json | eb0c8efdef5daaa64de158c7d0b8dd9f0d553ceeabf2d009671df85b8c4b2260 |
| sheet.csv | e5a6efae9e1a732f6500bd6c1a0ae5894734eb635f21dca65f3a33dfe782b684 |
| key.json | 99156e2c29f7211c2b8d798da957a17e5f7cefa61add3d08bb5d7bae4eadf88f |
| sheet.additional-ratings.csv | a5556d3650b9b10dd121973d3720b5b7a077825756c1c4b35977a8713d604d33 |
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
checklist, final quality scoring, operator-approved live configuration and V2 maintenance
verification remain open under #25163. No enforcement is authorized by this status report.

The harness passed 12 focused isolated tests, Ruff, scoped mypy, changed-test quality and
type audits, the suppression ratchet and project-aware plan validation. Existing pytest,
anyio and pytest-asyncio deprecation warnings remain a tracked validation finding.
