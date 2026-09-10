# Archify evidence

## Scope and identity

This capture uses the pinned project-local Archify source at
`sources/archify/archify` (frozen commit
`c6519401f7b91b9d43011657880893b0a8955548`) and the verified host
`gpt-5.6-terra` medium profile recorded in the frozen compatibility report.
Corpus data stayed in `corpora/archify/<case>`; generated material is isolated
under `results/archify`. No corpus source content or private repository URL is
published here.

The source grounding is deliberately at the evidence level: C1's manifest and
the inspected planning, replenishment, workbook, sync, lifecycle, recovery, and
fail-closed corpus paths informed the diagrams. Component-level source links
were intentionally omitted because the renderer requires public GitHub metadata
when such links are embedded; inventing that metadata would publish or misstate
private source provenance.

## Native C1 deliveries

| Diagram | Native specification | Delivered HTML | Specification SHA-256 | HTML SHA-256 | Validation |
| --- | --- | --- | --- | --- | --- |
| Repository architecture / application boundaries | `results/archify/C1/architecture/candidate.json` | `results/archify/C1/architecture/replenishment-boundaries.html` | `ce7395ea1f1d7dfb11cdc33312803b75ef2f40e84e6656a1a96adde43f4d9ee7` | `afd2e70d4bdc3e4e20d60848655b945d555faaa17b3fff98259961bb831eeaf7` | 9/9 showcase; 0 errors, 0 warnings |
| Weekly-planning approval workflow | `results/archify/C1/workflow/candidate.json` | `results/archify/C1/workflow/weekly-approval.html` | `553f93dfdd618770acdbd0fd4cdfcde2634d089a369fab83e1ae2221a60262a7` | `74309fc6ef1307183d79c143dc23278b0e6243b0186d7d5ec5142b51da024ca4` | 9/9 showcase; 0 errors, 0 warnings |
| Synchronization sequence | `results/archify/C1/sequence/candidate.json` | `results/archify/C1/sequence/synchronization.html` | `7bbee30287035f4920efc8f686f655641a6e0f85c0eacc86c758f7c6451d3cbd` | `489e14c125aa349193b45566bd3c5aec54f5368ffd0bfb605236923080793205` | 9/9 showcase; 0 errors, 0 warnings |
| Report / forecast data flow | `results/archify/C1/dataflow/candidate.json` | `results/archify/C1/dataflow/report-forecast.html` | `e8b99a3e9ee53f29442293df7df3286b8fe0c90ede0bb3e0775ecc3c2999991a` | `6f4701f514f6d3ce42e1b1e17dea9bb98c3c3e89894742974cad5b0816bb8358` | 9/9 showcase; 0 errors, 0 warnings |
| Operational run lifecycle / recovery | `results/archify/C1/lifecycle/candidate.json` | `results/archify/C1/lifecycle/run-recovery.html` | `b4383148b97e6112ab471eb4f40817e8e9b74822e784d972979e41c7c0dff5b5` | `7b314e84ee90b97fea2b7c0ec74536b14d5b3d9df3d5bebb9465c39723677ddb` | 9/9 showcase; 0 errors, 0 warnings |

Every final candidate was validated before delivery at quality `showcase`.
The renderer reported zero crossings, ambiguous corridors, composition errors,
and composition warnings for each delivery. Complete path and digest inventories
are `results/archify/inventory.txt` and `results/archify/inventory.sha256`.
`inventory.txt` is a lexically ordered, relative-path manifest of the 24 evidence
artifacts and excludes both inventory files. `inventory.sha256` repeats those 24
checks with absolute paths and appends the SHA-256 of `inventory.txt`, so verification
is nonrecursive:
`(cd results/archify && shasum -a 256 -c inventory.sha256 && shasum -a 256 -c inventory.txt)`.

### C1 native attempt measurements

The following are the exact metrics reported by the successful native
`validate` attempts immediately before the matching successful native `deliver`
attempts. Every attempt passed all 9 checks at `showcase`, with 0 composition
errors and 0 warnings. Delivery reproduced the HTML digests listed above.

| Diagram | Delivered bytes | Minimum label clearance | Max bends / over limit | Max stretch / over limit | Minimum segment / interior segment | Crossings / corridors / border runs / label issues / readability issues | Short / endpoint-short / interior-short / micro segments |
| --- | ---: | ---: | --- | --- | --- | --- | --- |
| Architecture | 709349 | 57.4 | 2 / 0 | 1 / 0 | 30 / 193 | 0 / 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| Workflow | 708345 | 82.4 | 2 / 0 | 1.216 / 0 | 16 / 148.4 | 0 / 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| Sequence | 704267 | 40 | 0 / 0 | 1 / 0 | 152.8 / null | 0 / 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| Dataflow | 703931 | 108.4 | 0 / 0 | 1 / 0 | 103 / null | 0 / 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| Lifecycle | 703330 | null | 0 / 0 | 1 / 0 | 36 / null | 0 / 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |

`minProjectedNodeTextPx` was `null` for all five renderer validation attempts.
The suggested limits were 2 bends per relationship, stretch 1.35, segment 16px,
and micro-segment 8px for every attempt.

## Change and bounded correction evidence

C3 records a committed-change candidate and delivery at
`results/archify/C3/architecture/`. Its delivered HTML SHA-256 is
`6bb9ae18c28e64c18ce27a29eb0b2024b9970704c814968d6becb4ae5df6163b`.
The native `compare` invocation was attempted and correctly rejected the base
candidate because its six original connections did not have authored stable
IDs. That diagnostic is preserved as demonstrated unsupported comparison input,
not replaced with a fabricated delta.

C7 preserves the invalid-candidate/last-good case at
`results/archify/C7/invalid-candidate/`. Adding a private corpus source link
caused native repository-evidence validation and delivery failure; `before.sha256`
and `after.sha256` are identical, proving failed delivery did not overwrite the
last-good HTML. `validation.json` and `delivery.json` contain the native failure
receipts.

## Browser, retrieval, and export disposition

`visual-check` was run for every delivered HTML and wrote sidecar receipts.
It is `skipped` rather than passed: the pinned renderer found no Chrome or
Chromium executable, so it produced no screenshots or browser measurements.
The delivery receipts remain valid deterministic artifact evidence; no
perceptual-review claim is made.

Archify's supported CLI is a renderer/validator. It has no native corpus
retrieval/Ask command and no standalone CLI export command; viewer export is a
browser capability and could not be exercised without Chrome. These are recorded
as demonstrated unavailable rather than substituted with fresh-source answers
or a non-native exporter.

## Reproduction

Run from `sources/archify/archify`:

```bash
node bin/archify.mjs validate <type> <candidate.json> --quality showcase --json
node bin/archify.mjs deliver <type> <candidate.json> <output.html> --quality showcase --json
node bin/archify.mjs visual-check <output.html> --json
```

`node scripts/check-update.mjs --json` returned `silent` with
`invalid-arguments`; no update was installed or acknowledged.
