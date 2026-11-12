# Expanded population and fixed-name Clef diagnostic

Task: #25163. Operator direction: expand beyond the 15-community pilot, author names
with Codex, test them with Clef, and make the human review understandable. The old
45 human ratings and all pilot artifacts are preserved unchanged.

## Installed graph correction

Source candidate `904ad0bd76fb2d3f50e1858eedda275b97fd8652` landed with generated
release tip `aa094a1baa37f00fab52cf302b48107a9da3f779`. The post-land command
`cargo build --release -p gobby-code` passed in 8m50s. The resulting `gcode 1.9.117`
was promoted through `promote_workspace_binary_set`, with only gcode supplied.
No daemon, ghook or gclient binary was promoted by this task.

- Installed signed gcode SHA256:
  `2594bbc9e8d546ec3fa4382bb95b33aa595f34cc555b70053528504162cd843c`.
- Recorded source identity:
  `3b4c0b3906cf7fa523f6181a8927d501f51b0255f47952cd0ffbd87d5537a0cd`.
- Release identity records pre-signing artifact SHA256
  `b95f145769d274c54927fdaef42673b8cfc0c90426997b947443c0e456addc44`;
  it differs from the signed installed bytes.

`gcode index /Users/josh/Projects/gobby --lock-wait-seconds 30 --format json`
passed: 9,468 files scanned, zero files re-extracted, communities refreshed rather
than skipped. It reported 3,003 communities, 141 changed, 23 new IDs and 97 retired
IDs. The algorithm-cache version change rebuilt the partition from existing facts.
The source fix's 100 focused Rust/database tests, Clippy and formatting evidence
remain in the [pre-fix audit](../population-study-2026-11-11/README.md).

| Measure | Frozen before | Corrected refresh |
| --- | ---: | ---: |
| Communities | 3,074 | 3,003 |
| Member files | 9,463 | 9,468 |
| Singletons | 2,915 | 2,810 |
| Pairs | 20 | 24 |
| Groups with at least three files | 139 | 169 |

Comparing the same paths: 109 former singletons joined groups; one grouped file
became a singleton. Five files were added and none removed between snapshots;
their exact paths are in `expanded-study-summary-20261111.json`. This is a live
before/after observation, not a perfectly frozen source experiment. The input-edge
defect did not explain every singleton: documents, configuration and files without
represented import connections remain common. No Graphify C1–C8 verdict is claimed.

## Names and coverage

`names.json` contains **169 names authored directly by Codex**, grounded in the
corrected membership and representative file paths. They cover 6,610 files.
No Gemma or other text-generation service generated these names. These are
provisional navigation names, not individually source-body-verified ownership claims.

`label-manifest.json.gz` maps all 3,003 community IDs and member signatures to:

- 169 authored group names;
- 2,810 explicitly derived single-file captions;
- 24 pairs of derived file captions, covering another 48 files.

The small-file captions come from 1,302 Markdown headings, 184 Python module
docstrings, 173 Rust module comments, and 1,199 readable filenames. These counts
describe 2,858 files, including both members of each pair. File captions are not
represented as 2,834 individually authored semantic group names. Some are weak:
“Deliverable” and “New.dump” are concrete examples retained in the diagnostic.
None of this manifest was written to live stored labels.

## Clef results

The exact research probe is `diagnostic.py.txt`; fixed inputs and parsed responses
are in `clef-diagnostic.json.gz`. It ran from 2026-11-12 01:18:45 to 01:23:18 UTC
against local Systemone at `http://127.0.0.1:11434`, model `clef:27b`, context
configuration 16,384, timeout 30 seconds. API charge was **$0**; local compute cost
was not measured. All recorded baseline backend identities were
`response:clef:27b`.

There were **313 completed cases and 699 parsed answers**, with zero reported
decision errors: 193 baseline requests with three questions each, then 120 carry
requests. Baselines covered every authored group name, 16 sampled singletons and
eight sampled pairs. Questions asked for usefulness, a choice against the stored
deterministic label and an unrelated restaurant name, and applicability of that
restaurant name. Candidate position alternated between a and b; each case was
tested in one position only. Questions within one request are correlated.

| Baseline kind | New candidate chosen | Mean usefulness, 0–2 |
| --- | ---: | ---: |
| Authored group names | 167/169 | 1.627 |
| Single-file captions | 10/16 | 1.263 |
| Paired-file captions | 5/8 | 1.539 |

No unrelated restaurant option won. Its mean applicability probability was 0.029,
with all 193 below 0.5. This is an easy negative control, not a realistic confusing
neighbor label. The weaker group candidates included “Native graph and vector
service health helpers” and “Shared web form controls and project selection”.

Carry selected six groups from each of five size strata using seed 25163. Every
variant judged the **same original name**; no regenerated name supplied carry truth.

| Membership variant | Cases with probability at least 0.5 | Mean probability |
| --- | ---: | ---: |
| Unchanged | 28/30 | 0.861 |
| Same files, reversed order | 28/30 | 0.834 |
| Remove roughly one quarter | 28/30 | 0.847 |
| Replace all with files from another top-level root | 0/30 | 0.037 |

Replacement donors are deterministic and intentionally remote; many share a donor.
This does not measure subtle neighboring-topic drift. Replacement group sizes can
be smaller when the donor has fewer files. The retained lists show every change.
The 0.5 split above is descriptive and **does not change the original 85% rollout
bar**. Neither model preferences nor these controls establish human precision,
recall, calibration or release acceptance. Full member paths were supplied, without
source bodies; this differs from production's 40-path/8,000-character representation.

## Human review and disposition

Read [expanded-review.md](expanded-review.md), then record the 20 answers in
`expanded-review.csv` or alongside the numbered items. It covers all five size
strata: ten baseline cases, five partial removals and five complete replacements.
The fixed seed selected cases without using Clef's answers. Every case shows its
candidate name, representative files and actual indexed code entry points.
The key is retained separately in `expanded-review-key.json`.

Accuracy and findability each have explicit 0/1/2 definitions. **Keep this exact
name? yes/no/skip** asks for direct carry truth, avoiding an implicit conversion of
partial scores. A skip never counts as approval. This is additional representative
review; it does not request redoing the previous 45 ratings.

All three live decision consumers remain **off**. This run does not prove immutable
per-response model identity or the backend token-context/truncation contract, and
does not replace the outstanding live maintenance/V2 evidence. No activation patch
was applied. Expanded human judgments and remaining backend qualification are still
needed before an enforcement ship verdict.

## Reproducibility and validation

The compressed artifacts are byte-preserving gzip containers. The corrected
population's uncompressed SHA256 is
`a55fbe192e940ad38d4865b09ab4165d5b56416b1c4ec3d912c539e4acc0eade`.
The diagnostic records hashes of that population, `names.json` and the manifest.
`freeze-population.py.txt`, `prepare-review.py.txt` and `summarize.py.txt` preserve
the exact research helpers, including their original workspace paths.

Direct validation passed: 3,003 unique IDs, 9,468 disjoint member paths, complete
manifest coverage, exactly 169 authored names, 313 unique case keys, unchanged
candidate names across carry variants, equal membership under reorder, strict
subset under removal and disjoint membership under replacement. All four research
scripts parse, all 20 review cases have captured source outlines, and the summary
checks every response for reported errors. This evidence-only addition changes no
production logic and requires no new unit tests.
