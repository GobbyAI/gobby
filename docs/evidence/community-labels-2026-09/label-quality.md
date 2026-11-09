# Community label quality

Ship decision for the no-decisions fallback study (#25817). The full decision
study (D/G/J and carry) has not run.

## Ship rule

**Rule 1.** G regressions 0/15, so generated community labels ship on and all
three decision checks stay off. The score proposes this configuration. It does
not apply it, and no live configuration changed.

```yaml
code_index.community_label.enabled: true
ai.decisions.community_label.mode: off
ai.decisions.community_label_carry.mode: off
ai.decisions.community_label_sufficiency.mode: off
```

Rule selection in `scripts/community_label_study.py` (`_score`, at b76336f4ea):
rule 1 when G regressions are 0. The bar was set to zero by #25817 commit
`b76336f4ea` (was one). Rule 2 needs a passing gate from a decision run; rule 3
is deterministic-only labeling. Neither applies to this run.

## Run provenance

- Plan: `.gobby/plans/community-label-decisions.md` (D4, ship rules).
- Branch: `--no-decisions`, because the OpenRouter item-4 checklist is unmet.
- Corpus: gobby only (`d45545c5-ded5-4335-b115-0245752edacf`). C1 was removed
  from the study by #25817 commit `16e7a8e780`.
- Sample: seed 22604, 15 eligible communities (floor 15). Each community is
  rated under both arms, giving 30 rows.
- Generation: 15 of 15 `written_model`, 0 failures. Decision usage: none.
  Schema rejection rate 0. Decision cost $0.
- Prepare was run by the Orchestrator on 2026-11-09. The developer seat did not
  run prepare. It rebuilt the rated sheet from the blinded sheet and Josh's
  ratings, and the rebuilt file matches the Assistant's rated file by digest.
- Rating distribution: accuracy 0/1/2 = 15/1/14; findability 0/1/2 = 14/10/6.

## Results

Score at `b76336f4ea`, script sha256
`22ed4642858a29a30d155694026339a2e9bd6258cbe7e52080223d1138b267d1`.

| Metric | Value | Bar | Result |
| --- | ---: | ---: | --- |
| accuracy_lift | undefined | >= 0.3 | FAIL |
| findability_lift | undefined | >= 0.5 | FAIL |
| j_regressions | undefined | <= 1 | FAIL |
| gate_precision | undefined | >= 0.85 | FAIL |
| gate_recall | undefined | >= 0.6 | FAIL |
| confidence_separation | undefined | >= 0.15 | FAIL |
| schema_rejection_rate | 0 | <= 0.1 | PASS |
| decision_cost_usd | 0 | <= 0.05 | PASS |
| carry_precision | undefined | >= 0.85 | FAIL |
| carry_recall | undefined | >= 0.6 | FAIL |
| sufficiency_precision | undefined | >= 0.85 | FAIL |
| sufficiency_recall | undefined | >= 0.6 | FAIL |

The lift, gate, carry and sufficiency metrics are undefined because this
branch ran no decision checks. The ship rule rests on G regressions and the
two passing metrics above.

## Artifacts

Files are in `study-2026-11/`. Raw rating records (`ratings-raw/`) are not
committed.

| File | Bytes | sha256 |
| --- | ---: | --- |
| `sheet.csv` | 56013 | `fdfc82ae59405cb5dd790e3e18cdd5effa921c21d5a4ff2b63dd794624b00bdc` |
| `key.json` | 7035 | `72b2501e79fdfa9fafb1c69508a2f5597ba687daee711ed311635939124de37f` |
| `run.json` | 9803 | `e619df88b175f9d2ab9136b759fcc6294d13a74fb1e32a2aa8e021f9b2f82153` |
| `sheet.rated.csv` | 56073 | `91b63c24cd99363b7a664b12ac2d956860bfebaa00f2343ff3ab7a65ae01bcba` |
| `ratings.csv` | 419 | `55e25e5a82e73fc23b524f01d2564941a7895d9306b6e853893b20de2da01b64` |

`sheet.rated.csv` is `sheet.csv` with the two rating columns filled from
`ratings.csv`, written with `csv.writer` defaults. Its item IDs, members and
names match `sheet.csv` row for row.

`key.json` and `run.json` are committed after the ratings are final, as the
Orchestrator directed. Committing `key.json` makes the arm mapping visible to
anyone with repository access, so a later rating round needs new key material.

## Open items

- The full decision study (D/G/J and carry) remains pending. It needs a genuine
  OpenRouter capture and its own rule evaluation.
- The proposed configuration needs operator approval before any live change.
