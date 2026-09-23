# Community partition churn

Q1.4 ran on 2026-09-22 against both required corpora in the isolated
`gobby_gcode_test` database. The harness copied source files to a temporary directory,
rebound only the isolated test checkout, performed edits in that copy, restored the
checkout root, and deleted the copy on exit. No file in either real corpus was changed.

Each run executed these phases:

1. Two unchanged single-file `gcode index` paths.
2. A direct `UPDATE` of the selected row to `label_source='model'`, with
   `labeled_signature=member_signature`, immediately before renaming three files in
   that community.
3. One added file.
4. One deleted singleton community plus a new, unrelated two-file community.

The focused command was:

```text
CARGO_BUILD_JOBS=4 nice -n 15 -- cargo nextest run -p gobby-code -E 'test(churn_evidence_experiment)' --status-level fail --final-status-level fail --no-capture
```

It passed for C1 in 2.14 seconds and for Gobby in 569.51 seconds.

## Gobby checkout

The two unchanged runs each returned 2,301 communities with `changed=0`,
`new_ids=0`, `retired_ids=0`, and `skipped_unchanged=true`; the stored rows, including
`refreshed_at`, were byte-for-byte equal through the Rust row model.

The rename selected community 1 and changed these paths:

| Before | After |
| --- | --- |
| `src/gobby/agents/dry_run.py` | `src/gobby/agents/dry_run_q14_renamed_0.py` |
| `src/gobby/agents/isolation_git_hygiene.py` | `src/gobby/agents/isolation_git_hygiene_q14_renamed_1.py` |
| `src/gobby/agents/lifecycle_checkout.py` | `src/gobby/agents/lifecycle_checkout_q14_renamed_2.py` |

The focal row diff was:

| Field | Before | After rename |
| --- | --- | --- |
| `community_id` | 1 | 1 |
| `member_signature` | `bfbe84defcea3287` | `213633e7bea0b292` |
| `member_count` | 191 | 191 |
| `internal_edges` | 599 | 599 |
| `cohesion` | 0.033011848994213285 | 0.033011848994213285 |
| `representatives` | `protocol.py`, `projects.py`, `isolated_checkout.py`, `project_context.py`, `project_checkouts.py` | unchanged |
| `boundary` examples | `2:148`, `3:118`, `4:153`, `7:121` | `2:143`, `3:112`, `4:150`, `7:87` |
| stored label | deterministic `tests` | model `Q1.4 model label` |
| `labeled_signature` | null before the direct update | `bfbe84defcea3287` |
| read `label_stale` | false after the direct update | true |
| read label/source | `Q1.4 model label` / model | `tests` / deterministic |

The rename report was 2,302 communities, 52 changed rows, one new ID, and no retired
ID. The full structured diff included membership, `member_signature`, representatives,
edges, cohesion, boundary, and label fields for every changed row; the focal row above
records the label-carry/staleness obligation.

Adding `q14_gobby_added.py` produced 15 changed rows with no new or retired ID. On
community 1 its `member_signature` advanced from `213633e7bea0b292` to
`716d9f8e8a933d2f`; the model label and original `labeled_signature` remained carried.

The final row diff retired singleton community 342:

```text
- id=342 member_signature=e522f0dd2f1b5c71 members=[build_backend/__init__.py]
+ id=2303 member_signature=c89ad7b7107b8fd9
  members=[q14_gobby_unrelated_a.py, q14_gobby_unrelated_b.py]
  internal_edges=1 cohesion=1.0 boundary=[]
```

The final report had `new_ids=1` and `retired_ids=1`; ID 342 was not reused.

## Frozen Game Goblins C1

The two unchanged runs each returned 57 communities with `changed=0`, `new_ids=0`,
`retired_ids=0`, and `skipped_unchanged=true`, again leaving every stored row and
`refreshed_at` unchanged.

The rename selected community 1 and changed:

| Before | After |
| --- | --- |
| `src/game_goblins/__init__.py` | `src/game_goblins/__init___q14_renamed_0.py` |
| `src/game_goblins/__main__.py` | `src/game_goblins/__main___q14_renamed_1.py` |
| `src/game_goblins/cli.py` | `src/game_goblins/cli_q14_renamed_2.py` |

The focal row diff was:

| Field | Before | After rename |
| --- | --- | --- |
| `community_id` | 1 | 1 |
| `member_signature` | `92473c80dd5a717e` | `a8d8c7009920c20f` |
| `member_count` | 30 | 28 |
| members | removed the three original paths | added `cli_q14_renamed_2.py`; the other two renamed files became singleton communities 58 and 59 |
| `internal_edges` | 83 | 73 |
| `cohesion` | 0.19080459770114944 | 0.1931216931216931 |
| `boundary` | `2:31`, `3:15`, `4:19`, `5:10` | `2:30`, `3:14`, `4:19`, `5:10` |
| representatives | `settings.py`, `replenishment/__init__.py`, `publish_lock.py`, `run_labels.py`, `weekly_store.py` | unchanged |
| stored label | deterministic `src/game_goblins` | model `Q1.4 model label` |
| `labeled_signature` | null before the direct update | `92473c80dd5a717e` |
| read `label_stale` | false after the direct update | true |
| read label/source | `Q1.4 model label` / model | deterministic label / deterministic |

The rename report was 59 communities, three changed rows, two new IDs, and no retired
ID. Adding `q14_c1_added.py` then changed community 58 from singleton signature
`a945fe90f732c298` to two-member signature `b5b166b9c6264e38`, with one internal edge
and no new or retired ID.

The final row diff retired singleton community 10 and allocated a fresh ID:

```text
- id=10 member_signature=c1b6e98617b74a13 members=[Buylist/buylist_automation.py]
+ id=60 member_signature=f0c128dd8bc57076
  members=[q14_c1_unrelated_a.py, q14_c1_unrelated_b.py]
  internal_edges=1 cohesion=1.0 boundary=[]
```

The final report had `new_ids=1` and `retired_ids=1`; ID 10 was not reused.
